"""Offline Browser Dry Run executor and optional Playwright session wrapper."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import time
from typing import Any, Protocol
from urllib.parse import urlparse

from .execution import ExecutionPlan, validate_execution_context
from .inspector import FormBindings, fingerprint_html
from .models import ApplicationContext, ValidationResult
from .inspection import (
    INSPECTION_STAGE_FORM_DISCOVERY,
    AuthorizedInspectionRequest,
)
from .qa import AFFIRM_MARKERS, AFFIRM_SOURCE, DECLINE_MARKERS, DECLINE_SOURCE


class BrowserSessionError(RuntimeError):
    pass


@dataclass
class NetworkRequestEvent:
    origin: str
    path_hash: str
    method: str
    resource_type: str
    allowed: bool
    reason: str = ""


def _origin_of(url: str) -> str:
    """Origem normalizada (esquema + host + porta nao padrao)."""
    parsed = urlparse(url)
    if not parsed.hostname:
        return ""
    origin = f"{parsed.scheme.casefold()}://{parsed.hostname.casefold()}"
    if parsed.port is not None and parsed.port not in {80, 443}:
        origin += f":{parsed.port}"
    return origin


@dataclass(frozen=True)
class AuthorizedWrite:
    """Permissao one-shot para UMA escrita especifica do browser.

    Substitui "desligar o guard": a sessao continua bloqueando toda escrita,
    exceto o POST exato que a SubmissionIntent autorizou. Depois de consumida,
    ``max_writes`` esgota e o guard volta a bloquear.
    """

    application_id: str
    submission_intent_id: str
    origin: str
    path_pattern: str
    method: str = "POST"
    max_writes: int = 1

    def covers(self, method: str, url: str) -> bool:
        if method.upper() != self.method.upper():
            return False
        parsed = urlparse(url)
        if self.origin.startswith("*."):
            # Padrao de host: usado para o storage do board, cujo bucket varia
            # por regiao (ex.: *.s3.amazonaws.com).
            suffix = self.origin[2:].casefold()
            host = (parsed.hostname or "").casefold()
            if not (host == suffix or host.endswith("." + suffix)):
                return False
        elif _origin_of(url) != self.origin.rstrip("/"):
            return False
        # `re.match`, nao `fullmatch`: o requirement do challenge-guard declara
        # padroes de PREFIXO ancorados (ex.: `^/getcaptcha/`), porque o id da
        # requisicao vem depois. Exigir o caminho inteiro recusaria justamente o
        # trafego real e o widget nunca carregaria.
        return re.match(self.path_pattern, parsed.path) is not None



@dataclass(frozen=True)
class ChallengeRuntimePermission:
    """Permissao para o runtime de um widget anti-bot carregar.

    Nao deriva de AuthorizedWrite de proposito: aquele significa "mutacao
    permitida" e tem orcamento de submissao. Este e trafego de um widget de
    terceiros, com orcamento PROPRIO, e existe apenas para o desafio poder
    aparecer — sem ele o CAPTCHA nem carrega e o humano nao tem o que resolver.

    O caminho e sempre declarado. Um caminho aberto (`^/.*$`) nao e aceito:
    wildcard de origem com caminho aberto autorizaria um dominio inteiro, que e
    exatamente o defeito que ja apareceu numa policy de upload.
    """

    provider: str
    origin: str
    path_pattern: str
    method: str = "POST"
    max_requests: int = 1

    def __post_init__(self) -> None:
        if not self.path_pattern or self.path_pattern in {r"^/.*$", "^/.*$", "^.*$"}:
            raise ValueError("challenge runtime permission requires a scoped path pattern")
        if self.max_requests < 1:
            raise ValueError("challenge runtime permission requires a positive budget")
        # O requirement do challenge-guard declara host puro; o guard compara
        # origem com esquema. Normalizar aqui evita um nao-match silencioso, que
        # e o tipo de falha que so aparece em producao com o widget bloqueado.
        # Inclui wildcard: sem esquema, `*.hcaptcha.com` nunca casaria com
        # `https://api.hcaptcha.com` e o runtime do widget ficaria bloqueado.
        if self.origin and "://" not in self.origin:
            object.__setattr__(self, "origin", f"https://{self.origin}")

    def covers(self, method: str, url: str) -> bool:
        if method.upper() != self.method.upper():
            return False
        parsed = urlparse(url)
        expected = urlparse(self.origin)
        rule = (expected.hostname or "").casefold()
        actual = (parsed.hostname or "").casefold()
        if rule.startswith("*."):
            suffix = rule[2:]
            if not (actual == suffix or actual.endswith("." + suffix)):
                return False
        elif actual != rule:
            return False
        if (parsed.scheme or "").casefold() != (expected.scheme or "https").casefold():
            return False
        # `re.match`, nao `fullmatch`: o requirement do challenge-guard declara
        # padroes de PREFIXO ancorados (ex.: `^/getcaptcha/`), porque o id da
        # requisicao vem depois. Exigir o caminho inteiro recusaria justamente o
        # trafego real e o widget nunca carregaria.
        return re.match(self.path_pattern, parsed.path) is not None


class NetworkWriteGuard:
    """Deny all browser write requests during a dry-run session."""

    READ_METHODS = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, allowed_hosts: set[str]):
        self.allowed_hosts = allowed_hosts
        self.events: list[NetworkRequestEvent] = []
        self._pending_reads: set[int] = set()
        self._authorized_writes: list[AuthorizedWrite] = []
        self._authorized_usage: list[int] = []
        # Orcamento separado do de escrita: uma inspecao read-only nao pode
        # consumir nem liberar credito de upload ou de submissao.
        self._authorized_inspections: list[AuthorizedInspectionRequest] = []
        self._inspection_usage: list[int] = []
        # Terceiro orcamento, independente dos outros dois: runtime de widget
        # anti-bot nunca consome nem libera credito de upload ou submissao.
        self._challenge_runtime: list[ChallengeRuntimePermission] = []
        self._challenge_usage: list[int] = []

    @property
    def authorized_write(self) -> AuthorizedWrite | None:
        return self._authorized_writes[0] if self._authorized_writes else None

    @property
    def authorized_writes_used(self) -> int:
        return sum(self._authorized_usage)

    @property
    def authorized_writes_remaining(self) -> int:
        if not self._authorized_writes:
            return 0
        return sum(
            max(permit.max_writes - used, 0)
            for permit, used in zip(self._authorized_writes, self._authorized_usage)
        )

    @property
    def authorized_write_usage(self) -> list[tuple[AuthorizedWrite, int]]:
        """Uso por permissao, na ordem em que foram armadas.

        Necessario quando ha permissoes simultaneas: o desafio anti-bot escreve
        para o provedor dele e o contador agregado deixaria de dizer se o POST
        da candidatura realmente saiu.
        """
        return list(zip(self._authorized_writes, self._authorized_usage))

    def arm_write(self, permit: AuthorizedWrite) -> None:
        self.arm_writes([permit])

    def arm_writes(self, permits: list[AuthorizedWrite]) -> None:
        """Autoriza escritas especificas; todo o resto segue bloqueado.

        Cada permissao tem orcamento proprio e independente: a subida do
        curriculo e o POST de submissao sao escritas distintas, ambas
        necessarias, e nenhuma delas libera qualquer outra escrita.
        """
        for permit in permits:
            if permit.max_writes < 1:
                raise ValueError("authorized write must allow at least one request")
        self._authorized_writes = list(permits)
        self._authorized_usage = [0] * len(permits)

    def disarm_write(self) -> None:
        """Revoga as permissoes mantendo o registro de uso (auditoria)."""
        self._authorized_writes = []

    def arm_inspections(self, permits: list[AuthorizedInspectionRequest]) -> None:
        """Autoriza requisicoes de inspecao read-only, com orcamento proprio."""
        for permit in permits:
            if permit.max_requests < 1:
                raise ValueError("authorized inspection must allow at least one request")
        self._authorized_inspections = list(permits)
        self._inspection_usage = [0] * len(permits)

    def disarm_inspections(self) -> None:
        self._authorized_inspections = []

    def arm_challenge_runtime(self, permits: list[ChallengeRuntimePermission]) -> None:
        for permit in permits:
            if permit.max_requests < 1:
                raise ValueError("challenge runtime permission requires a positive budget")
        self._challenge_runtime = list(permits)
        self._challenge_usage = [0] * len(permits)

    def disarm_challenge_runtime(self) -> None:
        self._challenge_runtime = []

    @property
    def challenge_runtime_used(self) -> int:
        return sum(self._challenge_usage)

    @property
    def challenge_runtime_usage(self) -> list[tuple[ChallengeRuntimePermission, int]]:
        return list(zip(self._challenge_runtime, self._challenge_usage))

    @property
    def inspections_used(self) -> int:
        return sum(self._inspection_usage)

    @property
    def inspection_usage(self) -> list[tuple[AuthorizedInspectionRequest, int]]:
        return list(zip(self._authorized_inspections, self._inspection_usage))

    def _inspect_authorized_request(self, method: str, url: str, body: str, resource_type: str) -> tuple[bool, str]:
        """Tenta cobrir um POST por uma permissao de inspecao.

        Devolve (coberto, motivo). O motivo sempre pertence ao conjunto fechado,
        porque e o que sobrevive a redacao na auditoria.
        """
        last_reason = "INSPECTION_OPERATION_NOT_ALLOWED"
        for position, permit in enumerate(self._authorized_inspections):
            if self._inspection_usage[position] >= permit.max_requests:
                last_reason = "INSPECTION_BUDGET_EXHAUSTED"
                continue
            verdict = permit.validate(
                method=method,
                url=url,
                body=body,
                stage=INSPECTION_STAGE_FORM_DISCOVERY,
            )
            if verdict.covered:
                self._inspection_usage[position] += 1
                return True, verdict.reason_token
            last_reason = verdict.reason_token
        return False, last_reason

    @property
    def blocked_writes(self) -> list[NetworkRequestEvent]:
        return [event for event in self.events if not event.allowed and (event.method not in self.READ_METHODS or event.resource_type == "websocket")]

    @property
    def pending_read_count(self) -> int:
        return len(self._pending_reads)

    def begin_read(self, request: Any) -> None:
        method = str(getattr(request, "method", "GET")).upper()
        resource_type = str(getattr(request, "resource_type", ""))
        if method in self.READ_METHODS and resource_type != "websocket":
            self._pending_reads.add(id(request))

    def finish_read(self, request: Any) -> None:
        self._pending_reads.discard(id(request))

    def inspect(self, request: Any) -> bool:
        method = str(getattr(request, "method", "GET")).upper()
        resource_type = str(getattr(request, "resource_type", ""))
        origin, path_hash = _audit_network_target(str(getattr(request, "url", "")))
        if resource_type == "websocket":
            self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, False, "websocket blocked in dry-run"))
            return False
        if method not in self.READ_METHODS:
            url = str(getattr(request, "url", ""))
            for position, permit in enumerate(self._authorized_writes):
                if self._authorized_usage[position] >= permit.max_writes:
                    continue
                if permit.covers(method, url):
                    self._authorized_usage[position] += 1
                    self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, True, "authorized submission write"))
                    return True
            # POST de inspecao read-only: a permissao depende do CONTEUDO
            # (operacao, tipo de documento e vinculo com a vaga), nao so de
            # metodo e URL. Vale apenas na fase de descoberta e tem orcamento
            # proprio, que nao se confunde com o de escrita.
            if self._authorized_inspections:
                body = ""
                try:
                    body = str(getattr(request, "post_data", "") or "")
                except Exception:
                    body = ""
                covered, token = self._inspect_authorized_request(method, url, body, resource_type)
                if covered:
                    self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, True, f"authorized read-only inspection: {token}"))
                    return True
                self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, False, token))
                return False
            # Runtime de widget anti-bot: orcamento proprio, origem e caminho
            # declarados pelo provider do desafio. Nao autoriza submissao, e a
            # submissao nao autoriza isto.
            for position, permit in enumerate(self._challenge_runtime):
                if self._challenge_usage[position] >= permit.max_requests:
                    continue
                if permit.covers(method, url):
                    self._challenge_usage[position] += 1
                    self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, True, f"authorized challenge runtime: {permit.provider}"))
                    return True
            reason = (
                "write method blocked in dry-run"
                if not self._authorized_writes and not self._challenge_runtime
                else "write is not covered by the authorized submission permit"
            )
            self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, False, reason))
            return False
        self.events.append(NetworkRequestEvent(origin, path_hash, method, resource_type, True))
        return True


def _audit_network_target(url: str) -> tuple[str, str]:
    """Keep network evidence free of literal paths, query strings and fragments."""
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").casefold()
    origin = f"{parsed.scheme.casefold()}://{hostname}" if hostname else ""
    try:
        if parsed.port is not None:
            origin += f":{parsed.port}"
    except ValueError:
        pass
    path_hash = hashlib.sha256((parsed.path or "/").encode("utf-8")).hexdigest()[:16]
    return origin, path_hash


class GuardedBrowserSession(Protocol):
    page: Any
    context: Any
    network_guard: NetworkWriteGuard
    guarded: bool

    def arm_writes(self, permits: list[AuthorizedWrite]) -> None: ...


class DOMStabilityGuard:
    """Wait for a minimum observation window, DOM quietness and idle reads.

    ``max_ms`` e o teto de paciencia, nao a exigencia: continua sendo preciso um
    periodo real de quietude (``quiet_ms``) para liberar. O teto e generoso
    porque um desafio de CAPTCHA vivo muda o DOM continuamente — com uma janela
    visivel isso e normal e nao significa que o formulario esteja instavel.
    """

    def __init__(self, quiet_ms: int = 250, min_observation_ms: int = 600, max_ms: int = 12000):
        self.quiet_ms = quiet_ms
        self.min_observation_ms = min_observation_ms
        self.max_ms = max_ms

    def wait(self, page: Any, pending_read_count: Any = None) -> None:
        script = """
            ({quiet, minObservation, maxWait}) => new Promise((resolve, reject) => {
                const root = document.documentElement || document;
                const started = performance.now();
                let lastMutation = started;
                let checkTimer;
                let maxTimer;
                const finish = () => {
                    clearTimeout(checkTimer);
                    clearTimeout(maxTimer);
                    observer.disconnect();
                    resolve(true);
                };
                const fail = () => {
                    clearTimeout(checkTimer);
                    observer.disconnect();
                    reject(new Error('DOM did not stabilize'));
                };
                const observer = new MutationObserver(() => {
                    lastMutation = performance.now();
                });
                observer.observe(root, {subtree: true, childList: true, attributes: true, characterData: true});
                const check = () => {
                    const now = performance.now();
                    if (now - started >= minObservation && now - lastMutation >= quiet) {
                        finish();
                        return;
                    }
                    checkTimer = setTimeout(check, Math.min(quiet, 50));
                };
                maxTimer = setTimeout(fail, maxWait);
                check();
            })
        """
        deadline = time.monotonic() + self.max_ms / 1000
        first_observation = True
        try:
            while True:
                remaining_ms = max(int((deadline - time.monotonic()) * 1000), 1)
                page.evaluate(script, {"quiet": self.quiet_ms, "minObservation": self.min_observation_ms if first_observation else 0, "maxWait": remaining_ms})
                if pending_read_count is None or pending_read_count() == 0:
                    return
                first_observation = False
                if time.monotonic() >= deadline:
                    raise BrowserSessionError("DOM_UNSTABLE: read requests did not settle")
        except Exception as exc:
            if isinstance(exc, BrowserSessionError):
                raise
            raise BrowserSessionError("DOM_UNSTABLE: form did not stabilize") from exc


def validate_navigation_url(url: str, allowed_hosts: set[str] | None = None, *, resource: bool = False) -> ValidationResult:
    """Reject local, private and non-web URLs before Playwright sees them."""
    parsed = urlparse(url)
    if resource and parsed.scheme.casefold() in {"data", "blob"}:
        return ValidationResult(True, "OK")
    if parsed.scheme.casefold() not in {"http", "https"}:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["only http and https URLs are allowed"])
    if not parsed.hostname or parsed.username or parsed.password:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["URL must contain a public hostname without credentials"])
    hostname = parsed.hostname.rstrip(".").casefold()
    normalized_hosts = {item.casefold().rstrip(".") for item in (allowed_hosts or set())}
    exact_hosts = {item for item in normalized_hosts if not item.startswith("*.")}
    wildcard_hosts = {item[2:] for item in normalized_hosts if item.startswith("*.")}
    if allowed_hosts and hostname not in exact_hosts and not any(hostname != suffix and hostname.endswith("." + suffix) for suffix in wildcard_hosts):
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["hostname is not in the approved navigation policy"])
    try:
        port = parsed.port
    except ValueError:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["invalid URL port"])
    if port is not None and port not in {80, 443}:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["only ports 80 and 443 are allowed"])
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".localhost") or hostname.endswith(".local"):
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["local hostnames are not allowed"])
    try:
        addresses = [ipaddress.ip_address(hostname)]
    except ValueError:
        try:
            addresses = [ipaddress.ip_address(item[4][0]) for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)]
        except (OSError, ValueError):
            return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["hostname could not be resolved safely"])
    blocked = [address for address in addresses if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved or address.is_unspecified or address.is_multicast]
    if blocked:
        return ValidationResult(False, "UNSAFE_NAVIGATION_URL", ["private, loopback, link-local or reserved addresses are not allowed"])
    return ValidationResult(True, "OK")


@dataclass
class BrowserExecutionResult:
    application_id: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    stopped_before_submit: bool = True
    status: str = "COMPLETED"
    initial_fingerprint: str = ""
    current_fingerprint: str = ""
    submission_attempted: bool = False
    network_writes_allowed: bool = False
    network_guard_active: bool = False
    blocked_write_count: int = 0
    blocked_websocket_count: int = 0
    pending_read_count: int = 0
    reason: str = ""


@dataclass
class DryRunAuditReport:
    application_id: str
    result: str
    executed_actions: int
    pending_actions: int
    initial_fingerprint: str
    current_fingerprint: str
    operations: list[dict[str, Any]] = field(default_factory=list)
    submission_attempted: bool = False
    network_writes_allowed: bool = False
    network_guard_active: bool = False
    blocked_write_count: int = 0
    blocked_websocket_count: int = 0
    pending_read_count: int = 0
    reason: str = ""


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def write_dry_run_report(path: str | Path, report: DryRunAuditReport) -> None:
    _write_json(Path(path), {key: value for key, value in report.__dict__.items()})


class BrowserExecutor:
    """Minimal executor contract; intentionally exposes no submit operation."""

    def execute(
        self,
        context: ApplicationContext,
        plan: ExecutionPlan,
        bindings: FormBindings,
        current_html: str | None = None,
        *,
        allow_review: bool = False,
    ) -> BrowserExecutionResult:
        raise NotImplementedError


class DryRunBrowserExecutor(BrowserExecutor):
    """Record browser operations without opening a browser or sending data."""

    def execute(
        self,
        context: ApplicationContext,
        plan: ExecutionPlan,
        bindings: FormBindings,
        current_html: str | None = None,
        *,
        allow_review: bool = False,
    ) -> BrowserExecutionResult:
        validation = validate_execution_context(context, plan, bindings, current_html, allow_review=allow_review)
        if not validation.valid:
            raise BrowserSessionError("refusing invalid execution plan: " + "; ".join(validation.errors))
        operations = []
        for action in plan.actions:
            if action.action_type == "fill":
                operations.append({"operation": "fill", "field_key": action.field_key, "value": action.value, "step": action.step})
            elif action.action_type == "upload":
                operations.append({"operation": "upload", "field_key": action.field_key, "attachment_path": action.attachment_path, "step": action.step})
        return BrowserExecutionResult(plan.application_id, operations, stopped_before_submit=True)


class OptionSelectionError(ValueError):
    """Motivo tipado de por que uma opcao de combobox nao pode ser escolhida."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _marker_text(value: str) -> str:
    """Normaliza o rotulo antes de comparar com marcadores.

    "I don't wish to answer" precisa virar "i don t wish to answer" para casar
    com os marcadores, que sao escritos sem apostrofo.
    """
    return re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()


def choose_option_index(labels: list[str], expected: str, intent: str = "") -> int:
    """Indice da opcao a selecionar. Funcao pura, sem browser.

    Consentimento e recusa nao sao pesquisaveis pelo texto da resposta: o rotulo
    real e a opcao do proprio ATS ("Yes", "Acknowledge/Confirm", "Decline To
    Self Identify"). Para os demais valores tenta igualdade e, se falhar,
    prefixo — o seletor de pais do telefone mostra "Brazil +55" para "Brazil".
    """
    if intent == AFFIRM_SOURCE:
        matches = [index for index, label in enumerate(labels) if any(marker in _marker_text(label) for marker in AFFIRM_MARKERS)]
    elif intent == DECLINE_SOURCE:
        matches = [index for index, label in enumerate(labels) if any(marker in _marker_text(label) for marker in DECLINE_MARKERS)]
    else:
        # Normaliza os DOIS lados: o rotulo real pode ter pontuacao que a
        # resposta nao tem, e vice-versa ("Brazilian Grading System (0-19)").
        expected_norm = _marker_text(expected)
        normalized = [_marker_text(label) for label in labels]
        if not expected_norm:
            raise OptionSelectionError("OPTION_NOT_FOUND_COMBOBOX_OPTION")
        matches = [index for index, label in enumerate(normalized) if label == expected_norm]
        if not matches:
            matches = [
                index
                for index, label in enumerate(normalized)
                if label.startswith(expected_norm) or expected_norm.startswith(label)
            ]
    if len(matches) == 1:
        return matches[0]
    raise OptionSelectionError("OPTION_NOT_FOUND_COMBOBOX_OPTION" if not matches else "AMBIGUOUS_COMBOBOX_OPTION")



#: Banners de consentimento que aparecem antes de qualquer interacao e cujo
#: overlay intercepta cliques no formulario.
_COOKIE_CONSENT_SELECTORS = (
    '[data-ui="cookie-consent"]',
    '[aria-label="Cookie Consent"]',
    '[id*="cookie" i]',
    '[class*="cookie" i]',
)
_CONSENT_ACCEPT_LABELS = (
    "Accept all", "Accept All", "Accept", "Aceitar tudo", "Aceitar",
    "Allow all", "I agree", "Got it", "Entendi",
)


def dismiss_cookie_consent(page: Any) -> bool:
    """Melhor-esforco: nunca pode derrubar o preenchimento.

    Dispensar o banner e conveniencia, nao requisito. Uma API de locator que o
    stub de teste nao implementa (ou qualquer mudanca do site) tem de resultar
    em "nao havia banner", jamais em excecao no meio do fill.
    """
    try:
        return _dismiss_cookie_consent(page)
    except Exception:
        return False


def _dismiss_cookie_consent(page: Any) -> bool:
    """Dispensa um banner de consentimento que intercepta cliques.

    Nao e contornar nada do board: e fechar o aviso que o proprio site mostra
    antes de qualquer interacao. Sem isso, o overlay do banner engole o clique
    no controle final — o agente tentava enviar e o clique nunca chegava ao
    botao. O clique e procurado DENTRO do dialogo, entao nunca cai no controle
    de submissao por engano.
    """
    for selector in _COOKIE_CONSENT_SELECTORS:
        try:
            dialogs = page.locator(selector)
            total = dialogs.count()
        except Exception:
            continue
        for index in range(min(total, 3)):
            dialog = dialogs.nth(index)
            try:
                if not dialog.is_visible():
                    continue
            except Exception:
                continue
            for label in _CONSENT_ACCEPT_LABELS:
                candidates = dialog.get_by_role("button", name=label, exact=False)
                if not candidates.count():
                    continue
                try:
                    candidates.first.click(timeout=3000)
                    return True
                except Exception:
                    continue
    return False

def _read_back(locator: Any) -> str | None:
    """Valor que realmente ficou no controle, ou None se nao der para ler.

    ``None`` e diferente de ``""``: um widget sem leitura suportada nao pode ser
    declarado vazio, senao a conferencia reprovaria formularios corretos.
    """
    try:
        return str(locator.input_value())
    except Exception:
        return None


def _confirm_typeahead(page: Any, locator: Any, value: str, field_key: str) -> bool:
    """Escolhe a sugestao quando o campo e um typeahead que se limpa sozinho.

    O widget "Current location" do Lever aceita a digitacao e depois apaga o
    texto se nenhuma sugestao for escolhida — o valor real vai num input
    escondido (``selectedLocation``). Digitar e sair deixa o campo obrigatorio
    vazio, o formulario fica ``:invalid`` e o navegador recusa o submit sem
    disparar evento algum. Devolve True quando havia sugestoes e uma foi
    escolhida.
    """
    container = locator.locator("xpath=..")
    results = container.locator(".dropdown-results")
    if results.count() == 0:
        return False
    locator.click()
    # O autocomplete consulta pelo texto digitado: a resposta completa
    # ("Springfield, Minas Gerais, Brazil") nao devolve sugestao alguma. O primeiro
    # segmento e a consulta que um humano digitaria; a escolha continua sendo
    # feita contra o valor completo. A digitacao precisa ser tecla a tecla: o
    # widget so dispara a busca (GET /searchLocations) em eventos de teclado.
    query = str(value).split(",")[0].strip() or str(value)
    locator.fill("")
    locator.press_sequentially(query, delay=60)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        # As sugestoes sao divs (`.dropdown-location`), nao itens de lista; o
        # container fica vazio quando nao ha resultado.
        options = results.locator("xpath=./*")
        visible = [options.nth(index) for index in range(options.count()) if options.nth(index).is_visible()]
        if visible:
            labels = [" ".join((option.inner_text() or "").split()) for option in visible]
            try:
                chosen = choose_option_index(labels, str(value))
            except OptionSelectionError:
                return False
            visible[chosen].click()
            return True
        page.wait_for_timeout(200)
    return False


def _write_and_confirm(locator: Any, value: str, field_key: str) -> None:
    """Escreve e confirma que o valor sobreviveu no DOM.

    O ``fill`` do Playwright nao garante que a escrita persista: um input
    controlado pelo React pode ser re-renderizado e voltar a ficar vazio entre o
    preenchimento e o submit. Sem esta conferencia o pipeline anunciava
    "formulario preenchido" com campos obrigatorios vazios, o navegador recusava
    o submit por validacao nativa e nenhum evento ``submit`` era disparado — ou
    seja, a falha era completamente silenciosa.
    """
    if not str(value).strip():
        locator.fill(str(value))
        return
    page = getattr(locator, "page", None)
    if page is not None:
        _confirm_typeahead(page, locator, value, field_key)
    for attempt in range(3):
        current = _read_back(locator)
        if current is None:
            # Widget sem leitura suportada: nao ha como conferir, mas tambem nao
            # ha motivo para reprovar um formulario correto.
            if attempt == 0:
                locator.fill(str(value))
            return
        if current.strip():
            return
        locator.fill(str(value))
        if page is not None:
            DOMStabilityGuard().wait(page)
    raise BrowserSessionError(f"VALUE_NOT_COMMITTED: {field_key}")


class PlaywrightFormFiller:
    """Fill only validated controls; this class intentionally has no submit method."""

    def fill(self, session: GuardedBrowserSession, context: ApplicationContext, plan: ExecutionPlan, bindings: FormBindings, audit_dir: str | Path | None = None, *, allow_review: bool = False) -> BrowserExecutionResult:
        if not isinstance(session, PlaywrightSessionManager) or not session.guarded or not isinstance(session.network_guard, NetworkWriteGuard):
            raise BrowserSessionError("Playwright filler requires a GuardedBrowserSession")
        page = session.page
        if not page.evaluate("() => window.__jobsearchDryRun === true"):
            raise BrowserSessionError("Playwright page is not attached to a dry-run guarded session")
        dismiss_cookie_consent(page)
        current_html = page.content()
        validation = validate_execution_context(context, plan, bindings, current_html, str(getattr(page, "url", "")), allow_review=allow_review)
        if not validation.valid:
            raise BrowserSessionError("refusing stale or invalid form: " + "; ".join(validation.errors))
        fields = {field.key: field for field in context.form.fields}
        operations: list[dict[str, Any]] = []
        audit_path = Path(audit_dir) if audit_dir else None
        if audit_path:
            if not context.form.artifact_root:
                raise BrowserSessionError("audit directory requires an application artifact root")
            artifact_root = Path(context.form.artifact_root).expanduser().resolve()
            audit_path = audit_path.expanduser().resolve()
            try:
                audit_path.relative_to(artifact_root)
            except ValueError as exc:
                raise BrowserSessionError("audit directory must be inside the application artifact root") from exc
            audit_path.mkdir(parents=True, exist_ok=True)
            os.chmod(audit_path, 0o700)
            page.screenshot(path=str(audit_path / "screenshot-before.png"))
            os.chmod(audit_path / "screenshot-before.png", 0o600)
            _write_json(audit_path / "form.json", {"application_id": context.application_id, "fingerprint": plan.form_fingerprint, "fields": [{"key": field.key, "label": field.label, "field_type": field.field_type, "required": field.required, "options": field.options} for field in context.form.fields]})
            _write_json(audit_path / "bindings.json", {"form_id": bindings.form_id, "root_locator": bindings.root_locator, "fields": [binding.__dict__ for binding in bindings.fields]})
            _write_json(audit_path / "execution-plan.json", {"application_id": plan.application_id, "provider": plan.provider, "form_fingerprint": plan.form_fingerprint, "actions": [{"action_type": action.action_type, "field_key": action.field_key, "sha256": action.sha256} for action in plan.actions]})
        initial_fingerprint = plan.form_fingerprint
        for index, action in enumerate(plan.actions):
            field = fields[action.field_key]
            binding = bindings.for_field(action.field_key)
            if binding is None:
                raise BrowserSessionError(f"missing binding: {action.field_key}")
            locator = page.locator(binding.locator)
            if locator.count() != 1:
                raise BrowserSessionError(f"locator is not unique: {action.field_key}")
            field_type = field.field_type.casefold().strip()
            if action.action_type == "upload":
                locator.set_input_files(action.attachment_path)
                operations.append({"operation": "upload", "field_key": action.field_key})
            elif action.action_type == "fill":
                try:
                    self._fill_value(page, locator, binding, field, action.value, lambda: session.network_guard.pending_read_count)
                except BrowserSessionError as exc:
                    combo_stop_prefixes = (
                        "AMBIGUOUS_COMBOBOX_",
                        "UNSUPPORTED_COMBOBOX_",
                        "OPTION_NOT_FOUND_COMBOBOX_OPTION:",
                        "unsupported combobox",
                        "combobox value is empty",
                        "combobox listbox is not bound",
                    )
                    if field_type != "combobox" or not str(exc).startswith(combo_stop_prefixes):
                        raise
                    reason = str(exc)
                    report = self._report(session, plan, operations, "UNSUPPORTED_FORM", index, initial_fingerprint, "", reason, action_not_executed=True)
                    if audit_path:
                        self._persist_audit(page, audit_path, operations, report)
                    return BrowserExecutionResult(
                        application_id=plan.application_id,
                        operations=operations,
                        stopped_before_submit=True,
                        status="UNSUPPORTED_FORM",
                        initial_fingerprint=initial_fingerprint,
                        current_fingerprint="",
                        submission_attempted=False,
                        network_writes_allowed=False,
                        network_guard_active=True,
                        blocked_write_count=report.blocked_write_count,
                        blocked_websocket_count=report.blocked_websocket_count,
                        pending_read_count=report.pending_read_count,
                        reason=reason,
                    )
                operations.append({"operation": "fill", "field_key": action.field_key})
            else:  # defensive; validate_execution_context already rejects it
                raise BrowserSessionError(f"unsupported dry-run action: {action.action_type}")
            try:
                DOMStabilityGuard().wait(page, lambda: session.network_guard.pending_read_count)
            except BrowserSessionError:
                report = self._report(session, plan, operations, "DOM_UNSTABLE", index, initial_fingerprint, "")
                if audit_path:
                    self._persist_audit(page, audit_path, operations, report)
                return BrowserExecutionResult(plan.application_id, operations, True, "DOM_UNSTABLE", initial_fingerprint, "", False, False, True, report.blocked_write_count, report.blocked_websocket_count, report.pending_read_count)
            current_html = page.content()
            current_validation = validate_execution_context(context, plan, bindings, current_html, str(getattr(page, "url", "")), allow_review=allow_review)
            if not current_validation.valid:
                try:
                    current_fingerprint = fingerprint_html(context.form, bindings, current_html, str(getattr(page, "url", "")))
                except Exception:
                    current_fingerprint = ""
                report = self._report(session, plan, operations, "FORM_CHANGED", index, initial_fingerprint, current_fingerprint)
                if audit_path:
                    self._persist_audit(page, audit_path, operations, report)
                return BrowserExecutionResult(plan.application_id, operations, True, "FORM_CHANGED", initial_fingerprint, current_fingerprint, False, False, True, report.blocked_write_count, report.blocked_websocket_count, report.pending_read_count)
        self._repair_missing_values(page, plan, fields, bindings, lambda: session.network_guard.pending_read_count)
        report = self._report(session, plan, operations, "COMPLETED", len(plan.actions) - 1, initial_fingerprint, initial_fingerprint)
        result = BrowserExecutionResult(plan.application_id, operations, True, "COMPLETED", initial_fingerprint, initial_fingerprint, False, False, True, report.blocked_write_count, report.blocked_websocket_count, report.pending_read_count)
        if audit_path:
            self._persist_audit(page, audit_path, operations, report)
        return result

    @staticmethod
    def _report(
        session: GuardedBrowserSession,
        plan: ExecutionPlan,
        operations: list[dict[str, Any]],
        result: str,
        index: int,
        initial: str,
        current: str,
        reason: str = "",
        action_not_executed: bool = False,
    ) -> DryRunAuditReport:
        guard = session.network_guard
        pending_actions = max(len(plan.actions) - index - 1 + int(action_not_executed), 0)
        return DryRunAuditReport(
            plan.application_id,
            result,
            len(operations),
            pending_actions,
            initial,
            current,
            operations,
            False,
            False,
            True,
            len(guard.blocked_writes),
            sum(event.resource_type == "websocket" for event in guard.blocked_writes),
            guard.pending_read_count,
            reason,
        )

    @staticmethod
    def _persist_audit(page: Any, audit_path: Path, operations: list[dict[str, Any]], report: DryRunAuditReport) -> None:
        page.screenshot(path=str(audit_path / "screenshot-after.png"))
        os.chmod(audit_path / "screenshot-after.png", 0o600)
        _write_json(audit_path / "operations.json", operations)
        write_dry_run_report(audit_path / "dry-run-report.json", report)

    @staticmethod
    def _missing_values(page: Any, plan: Any, fields: dict[str, Any], bindings: Any) -> list[tuple[Any, Any, Any, Any]]:
        """Acoes de preenchimento cujo valor nao esta no DOM."""
        missing: list[tuple[Any, Any, Any, Any]] = []
        for action in plan.actions:
            if action.action_type != "fill" or not str(action.value).strip():
                continue
            field = fields[action.field_key]
            if field.field_type.casefold().strip() in {"combobox", "radio", "checkbox"}:
                continue
            binding = bindings.for_field(action.field_key)
            if binding is None:
                continue
            locator = page.locator(binding.locator)
            if locator.count() != 1:
                continue
            current = _read_back(locator)
            if current is not None and not current.strip():
                missing.append((action, field, binding, locator))
        return missing

    def _repair_missing_values(self, page: Any, plan: Any, fields: dict[str, Any], bindings: Any, pending_read_count: Any = None) -> None:
        """Reescreve os campos que se perderam e so entao confirma.

        Um widget controlado aceita a digitacao e depois limpa o campo sozinho:
        o typeahead de "Current location" do Lever faz isso quando nenhuma
        sugestao e escolhida, e o autopreenchimento do proprio board reescreve
        ``email``/``location`` quando a resposta do /parseResume chega depois do
        nosso preenchimento. Sem esta conferencia o pipeline anunciava
        "formulario preenchido" com campos obrigatorios vazios, o navegador
        recusava o submit por validacao nativa e nenhum evento ``submit`` era
        disparado — a falha era completamente silenciosa.
        """
        for _ in range(3):
            DOMStabilityGuard().wait(page, pending_read_count)
            missing = self._missing_values(page, plan, fields, bindings)
            if not missing:
                return
            for action, field, binding, locator in missing:
                self._fill_value(page, locator, binding, field, action.value, pending_read_count)
        DOMStabilityGuard().wait(page, pending_read_count)
        remaining = [action.field_key for action, _field, _binding, _locator in self._missing_values(page, plan, fields, bindings)]
        if remaining:
            raise BrowserSessionError(f"VALUE_NOT_COMMITTED: {', '.join(remaining)}")

    @staticmethod
    def _fill_value(page: Any, locator: Any, binding: FormBindings | Any, field: Any, value: Any, pending_read_count: Any = None) -> None:
        field_type = field.field_type.casefold().strip()
        if field_type == "select":
            option_value = binding.option_values.get(str(value))
            if option_value is None:
                raise BrowserSessionError(f"select option is not bound: {field.key}")
            locator.select_option(option_value)
            # Um select obrigatorio que continua no placeholder ("Select...")
            # deixa o formulario :invalid e o navegador recusa o submit sem
            # disparar evento algum; conferir o valor evita o falso "preenchido".
            current = _read_back(locator)
            if str(value).strip() and current is not None and not current.strip():
                raise BrowserSessionError(f"VALUE_NOT_COMMITTED: {field.key}")
        elif field_type == "combobox":
            if binding.multiple or field.multiple:
                raise BrowserSessionError(f"multiple combobox is unsupported: {field.key}")
            if binding.autocomplete not in {"", "none", "list", "both"}:
                raise BrowserSessionError(f"unsupported combobox autocomplete mode: {field.key}")
            expected = " ".join(str(value).split()).casefold()
            if not expected:
                raise BrowserSessionError(f"combobox value is empty: {field.key}")
            # Consentimento e recusa nao sao valores pesquisaveis: o rotulo real
            # e a opcao do ATS ("Yes", "Acknowledge/Confirm", "Decline to
            # self-identify"). Digitar o texto da resposta no filtro do
            # combobox zeraria a lista de opcoes.
            intent = str(getattr(getattr(field, "answer", None), "source", ""))
            pick_from_list = intent in {AFFIRM_SOURCE, DECLINE_SOURCE}
            locator.click()
            if binding.autocomplete in {"list", "both"} and not pick_from_list:
                locator.fill(str(value))
            DOMStabilityGuard().wait(page, pending_read_count)
            controls = str(locator.get_attribute("aria-controls") or locator.get_attribute("aria-owns") or "").split()
            if len(controls) > 1:
                raise BrowserSessionError(f"AMBIGUOUS_COMBOBOX_LISTBOX: {field.key} has multiple associated listboxes")
            listbox_id = controls[0] if controls else binding.listbox_id
            if not listbox_id:
                raise BrowserSessionError(f"UNSUPPORTED_COMBOBOX_LISTBOX_UNBOUND: {field.key}")
            listboxes = page.get_by_role("listbox")
            visible_boxes = [
                listboxes.nth(index)
                for index in range(listboxes.count())
                if listboxes.nth(index).is_visible()
                and listboxes.nth(index).get_attribute("id") == listbox_id
            ]
            if len(visible_boxes) != 1:
                raise BrowserSessionError(f"AMBIGUOUS_COMBOBOX_LISTBOX: {field.key}")
            options = visible_boxes[0].get_by_role("option")
            visible_options: list[tuple[str, Any]] = []
            for index in range(options.count()):
                option = options.nth(index)
                if option.is_visible():
                    visible_options.append((" ".join((option.inner_text() or "").split()).casefold(), option))
            try:
                chosen = choose_option_index([label for label, _ in visible_options], expected, intent)
            except OptionSelectionError as exc:
                raise BrowserSessionError(f"{exc.code}: {field.key}") from None
            visible_options[chosen][1].click()
        elif field_type == "radio":
            option_locator = binding.option_locators.get(str(value))
            if not option_locator:
                raise BrowserSessionError(f"radio option is not bound: {field.key}")
            page.locator(option_locator).check()
        elif field_type == "checkbox":
            if field.semantic_type == "checkbox_boolean":
                normalized = str(value).casefold()
                if normalized in {"true", "yes", "1", "on", "checked"}:
                    locator.check()
                else:
                    locator.uncheck()
            else:
                selected = value if isinstance(value, list) else str(value).split(",")
                for label, option_locator in binding.option_locators.items():
                    option = page.locator(option_locator)
                    if label in selected:
                        option.check()
                    else:
                        option.uncheck()
        else:
            _write_and_confirm(locator, str(value), field.key)


class PlaywrightSessionManager:
    """Small optional session wrapper; intentionally has no submit operation."""

    def __init__(self, headless: bool = True, allowed_hosts: set[str] | None = None, allowed_resource_hosts: set[str] | None = None):
        self.headless = headless
        self.allowed_hosts = allowed_hosts
        #: Hosts de terceiros que a propria pagina carrega (Google reCAPTCHA,
        #: fontes, CDN). Valem apenas para LEITURA: escrita continua sob o guard.
        self.allowed_resource_hosts = allowed_resource_hosts
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.network_guard: NetworkWriteGuard | None = None
        self.guarded = False
        self._client_write_armed = False

    _DRY_RUN_INIT_SCRIPT = """
        (() => {
          window.__jobsearchDryRun = true;
          // O bloqueio no cliente e apenas defesa em profundidade: ele nao tem
          // como saber se a escrita foi autorizada. Quem decide de fato e o
          // NetworkWriteGuard, que valida origem, caminho, metodo e o limite de
          // escritas. Enquanto um permit esta armado, o submit nativo e
          // liberado para que a requisicao exista e seja auditada — em vez de
          // morrer dentro do JavaScript do board e o clique nao produzir nada.
          const armed = () => window.__jobsearchWriteArmed === true;
          const blocked = () => { throw new Error('blocked by jobsearch-agent dry-run'); };
          if (window.HTMLFormElement) {
            const nativeSubmit = window.HTMLFormElement.prototype.submit;
            const nativeRequestSubmit = window.HTMLFormElement.prototype.requestSubmit;
            window.HTMLFormElement.prototype.submit = function (...args) {
              if (!armed()) blocked();
              return nativeSubmit.apply(this, args);
            };
            window.HTMLFormElement.prototype.requestSubmit = function (...args) {
              if (!armed()) blocked();
              return nativeRequestSubmit.apply(this, args);
            };
          }
          document.addEventListener('submit', event => {
            if (armed()) return;
            event.preventDefault();
            event.stopImmediatePropagation();
          }, true);
          if (navigator.sendBeacon) navigator.sendBeacon = () => false;
          if (window.WebSocket) window.WebSocket = function() { throw new Error('websocket blocked by jobsearch-agent dry-run'); };
        })();
    """

    def _guard_route(self, route: Any) -> None:  # pragma: no cover - exercised with Playwright installed
        request_url = route.request.url
        parsed = urlparse(request_url)
        if parsed.scheme.casefold() in {"data", "blob"}:
            frame_url = getattr(route.request.frame, "url", "")
            frame_validation = validate_navigation_url(frame_url, self.allowed_hosts, resource=False)
            if frame_validation.valid:
                route.continue_()
            else:
                route.abort("blockedbyclient")
            return
        # _guard_route avalia os recursos que a pagina carrega. Hosts de
        # terceiros (Google reCAPTCHA, fontes, CDN) entram como LEITURA: sem o
        # script do reCAPTCHA a pagina nao completa performAssessment() e o
        # submit nunca dispara. Escrita continua sob o NetworkWriteGuard.
        policy_hosts = self.allowed_hosts
        if self.allowed_resource_hosts:
            policy_hosts = set(self.allowed_hosts or set()) | set(self.allowed_resource_hosts)
        validation = validate_navigation_url(request_url, policy_hosts, resource=True)
        if not validation.valid:
            if self.network_guard:
                origin, path_hash = _audit_network_target(request_url)
                self.network_guard.events.append(NetworkRequestEvent(origin, path_hash, str(getattr(route.request, "method", "GET")), str(getattr(route.request, "resource_type", "")), False, "; ".join(validation.errors)))
            route.abort("blockedbyclient")
            return
        network_allowed = self.network_guard.inspect(route.request) if self.network_guard else False
        if network_allowed:
            if self.network_guard:
                self.network_guard.begin_read(route.request)
            route.continue_()
        else:
            route.abort("blockedbyclient")

    def _block_websocket(self, websocket: Any) -> None:  # pragma: no cover - exercised with Playwright installed
        origin, path_hash = _audit_network_target(str(getattr(websocket, "url", "")))
        if self.network_guard:
            self.network_guard.events.append(NetworkRequestEvent(origin, path_hash, "GET", "websocket", False, "websocket blocked in dry-run"))
        # Deliberately do not call websocket.connect().
        return

    def start(self) -> None:
        if not self.allowed_hosts:
            raise BrowserSessionError("Playwright session requires an explicit allowed_hosts policy")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise BrowserSessionError("Playwright is not installed") from exc
        self._playwright = sync_playwright().start()
        # Argumentos extra vem do ambiente para resolver problemas de plataforma
        # sem recompilar nada. O caso real: numa sessao Wayland o Chromium abre a
        # janela numa superficie que nunca chega ao desktop do usuario, e o
        # humano nao consegue resolver o CAPTCHA de uma janela que nao ve —
        # `JOBSEARCH_BROWSER_ARGS="--ozone-platform=x11"`.
        extra_args = [item for item in os.environ.get("JOBSEARCH_BROWSER_ARGS", "").split() if item]
        self.browser = self._playwright.chromium.launch(headless=self.headless, args=extra_args or None)
        self.context = self.browser.new_context(service_workers="block")
        self.network_guard = NetworkWriteGuard(self.allowed_hosts)
        self.context.add_init_script(self._DRY_RUN_INIT_SCRIPT)
        self.context.route("**/*", self._guard_route)
        if not hasattr(self.context, "route_web_socket"):
            self.close()
            raise BrowserSessionError("Playwright >= 1.48 with route_web_socket is required")
        self.context.route_web_socket("**", self._block_websocket)
        self.context.on("requestfinished", self._finish_read_request)
        self.context.on("requestfailed", self._finish_read_request)
        self.page = self.context.new_page()
        self.page.on("framenavigated", self._reapply_client_write_flag)
        self.guarded = True

    def arm_authorized_write(self, permit: AuthorizedWrite) -> None:
        """Autoriza exatamente uma escrita do browser nesta sessao."""
        self.arm_writes([permit])

    def arm_inspections(self, permits: list[AuthorizedInspectionRequest]) -> None:
        """Autoriza requisicoes de inspecao read-only (fase de descoberta).

        Nao liga o flag de submit nativo: a pagina pode buscar o schema, mas
        continua incapaz de enviar o formulario enquanto nao houver um permit de
        submissao.
        """
        if not self.guarded or self.network_guard is None:
            raise BrowserSessionError("cannot authorize an inspection on an unguarded session")
        self.network_guard.arm_inspections(list(permits))

    def disarm_inspections(self) -> None:
        if self.network_guard is not None:
            self.network_guard.disarm_inspections()

    def arm_writes(self, permits: list[AuthorizedWrite]) -> None:
        """Autoriza escritas especificas do browser, cada uma com seu orcamento."""
        if not self.guarded or self.network_guard is None:
            raise BrowserSessionError("cannot authorize a write on an unguarded session")
        self.network_guard.arm_writes(list(permits))
        self._set_client_write_flag(bool(permits))

    def disarm_authorized_write(self) -> None:
        if self.network_guard is not None:
            self.network_guard.disarm_write()
        self._set_client_write_flag(False)

    def _set_client_write_flag(self, armed: bool) -> None:
        """Sincroniza o bloqueio client-side com a existencia de um permit.

        Sem isto o script de init cancelava todo submit nativo da pagina mesmo
        durante uma submissao autorizada, e o ATS que envia o formulario pela
        API nativa do HTML nunca produzia requisicao alguma.
        """
        self._client_write_armed = armed
        if self.page is None:
            return
        try:  # pragma: no cover - depende de um browser real
            self.page.evaluate("(value) => { window.__jobsearchWriteArmed = value; }", armed)
        except Exception:
            # Uma pagina ja navegada/fechada nao invalida a autorizacao: o
            # NetworkWriteGuard continua sendo quem decide.
            return

    def _reapply_client_write_flag(self, frame: Any = None) -> None:
        """Reaplica o estado a cada documento novo.

        O flag vive no documento, nao na sessao: o pipeline arma o permit de
        upload antes de navegar, e sem esta reaplicacao o documento seguinte
        voltaria a bloquear o submit nativo apesar do permit armado.
        """
        if frame is not None and getattr(self, "page", None) is not None and frame is not self.page.main_frame:
            return
        if self.page is None:
            return
        try:  # pragma: no cover - depende de um browser real
            self.page.evaluate("(value) => { window.__jobsearchWriteArmed = value; }", self._client_write_armed)
        except Exception:
            return

    def open(self, url: str) -> None:
        if self.page is None:
            raise BrowserSessionError("Playwright session is not started")
        validation = validate_navigation_url(url, self.allowed_hosts)
        if not validation.valid:
            raise BrowserSessionError("refusing unsafe navigation: " + "; ".join(validation.errors))
        self.page.goto(url, wait_until="domcontentloaded")
        final_validation = validate_navigation_url(self.page.url, self.allowed_hosts)
        if not final_validation.valid:
            raise BrowserSessionError("navigation redirected to an unsafe URL")

    def close(self) -> None:
        if self.browser is not None:
            if self.context is not None:
                self.context.close()
            self.browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self.browser = None
        self.context = None
        self.page = None
        self._playwright = None
        self.network_guard = None
        self.guarded = False

    def _finish_read_request(self, request: Any) -> None:
        if self.network_guard:
            self.network_guard.finish_read(request)
