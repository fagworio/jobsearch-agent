"""ATS controlado que sabe **simular desafio** — com o `challenge-guard` real.

O E2E controlado original prova o caminho felizesem desafio. Este acrescenta os
três estados que o guard realmente distingue (verificado contra o guard v0.1.0):

    marcador `g-recaptcha` presente          -> needs_human
    marcador removido (a pessoa resolveu)    -> resolved_externally
    escrita enviada + página de recusa       -> provider_rejected

Não há dublê do guard em lugar nenhum: quem observa é a biblioteca real, no
Chromium real. O que o teste controla é o DOM e a resposta HTTP do board.
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading

from tests.e2e.controlled_ats import MultipartPart, ReceivedSubmission, parse_multipart

JOB_ID = "integration-001"

FORM_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("job_application[first_name]", "First name", "text", True),
    ("job_application[last_name]", "Last name", "text", True),
    ("job_application[email]", "Email", "email", True),
)

REQUIRED_FIELDS: tuple[str, ...] = tuple(name for name, _label, _kind, required in FORM_FIELDS if required)

#: Marcador que o guard reconhece como reCAPTCHA. O `id` é o que o teste remove
#: para simular a pessoa resolvendo o desafio.
CHALLENGE_MARKER = (
    '<div id="gate-challenge" class="g-recaptcha" data-sitekey="6Lc-controlled" '
    'style="width:304px;height:78px;border:1px solid #ccc">I am not a robot</div>'
)


def _control(name: str, label: str, kind: str, required: bool) -> str:
    flag = " required" if required else ""
    return f'<label for="{_control_id(name)}">{label}</label><input id="{_control_id(name)}" name="{name}" type="{kind}"{flag}>'


def _control_id(name: str) -> str:
    match = re.search(r"\[([^\]]+)\]", name)
    return match.group(1) if match else name


def render_form(*, challenge: bool) -> str:
    """Formulario com o comportamento do board real.

    Enquanto o desafio esta na pagina, o submit e RECUSADO no cliente — que e o
    que um widget anti-bot faz (o SPA nao envia sem o token). Sem isso o ATS
    controlado aceitaria o POST com o desafio presente, e o cenario mediria
    outra coisa que nao a realidade.
    """
    controls = "\n      ".join(_control(*spec) for spec in FORM_FIELDS)
    marker = CHALLENGE_MARKER if challenge else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Apply — Controlled Integration ATS</title></head>
<body>
  <h1>WordPress Developer</h1>
  <form id="application_form" action="/jobs/{JOB_ID}/apply" method="post" enctype="multipart/form-data">
      {controls}
      <label for="resume">Resume</label>
      <input id="resume" name="job_application[resume]" type="file" accept="application/pdf">
      {marker}
      <div class="error" role="alert" id="gate-error" hidden>Please complete the recaptcha.</div>
      <button type="submit">Submit application</button>
  </form>
  <script>
    document.getElementById('application_form').addEventListener('submit', (event) => {{
      if (document.getElementById('gate-challenge')) {{
        event.preventDefault();
        document.getElementById('gate-error').hidden = false;
      }}
    }});
    const gateChallenge = document.getElementById('gate-challenge');
    if (gateChallenge) {{
      // Um clique humano no widget resolve: o marcador sai da pagina. E o que
      // o guard passa a reportar como `resolved_externally`.
      gateChallenge.addEventListener('click', () => gateChallenge.remove());
    }}
  </script>
</body></html>"""


def render_confirmation() -> str:
    return (
        "<!doctype html><html><body><h1>Thank you for applying</h1>"
        "<p>We received your application.</p></body></html>"
    )


def render_rejected() -> str:
    """Página de recusa pós-escrita: erro visível **e** desafio de novo."""
    return (
        "<!doctype html><html><body><h1>There was an error</h1>"
        '<div class="error" role="alert">There was an error verifying your application. '
        "Please try again.</div>"
        f"{CHALLENGE_MARKER}"
        "</body></html>"
    )


class ChallengeCapableATS:
    """Servidor de teste com desafio opcional.

    `challenge_before=True`  → a página do formulário traz o marcador;
    `reject_after_write=True` → a resposta ao POST é a página de recusa, mas o
                               POST **é** contado: a candidatura saiu.
    """

    def __init__(self, *, challenge_before: bool = False, reject_after_write: bool = False) -> None:
        self.challenge_before = challenge_before
        self.reject_after_write = reject_after_write
        self.submissions: list[ReceivedSubmission] = []
        self.gets: list[str] = []
        self.posts: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- URLs ---------------------------------------------------------------

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def apply_path(self) -> str:
        return f"/jobs/{JOB_ID}/apply"

    def url(self, path: str) -> str:
        return self.origin + path

    @property
    def apply_url(self) -> str:
        return self.url(self.apply_path)

    # -- ciclo de vida -------------------------------------------------------

    def __enter__(self) -> "ChallengeCapableATS":
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _send(self, status: int, body: bytes, content_type: str, headers: dict[str, str] | None = None) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 - nome do protocolo
                owner.gets.append(self.path)
                if self.path == owner.apply_path:
                    body = render_form(challenge=owner.challenge_before).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def do_POST(self) -> None:  # noqa: N802 - nome do protocolo
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                content_type = self.headers.get("Content-Type", "")
                owner.posts.append(self.path)
                if self.path != owner.apply_path:
                    self._send(404, b"not found", "text/plain; charset=utf-8")
                    return
                submission = ReceivedSubmission(
                    path=self.path,
                    content_type=content_type,
                    body=body,
                    parts=parse_multipart(content_type, body)
                    if "multipart/form-data" in content_type.casefold()
                    else (),
                )
                owner.submissions.append(submission)
                missing = [name for name in REQUIRED_FIELDS if not submission.field(name).strip()]
                if missing:
                    self._send(422, f"<h1>Rejected</h1><p>{', '.join(missing)}</p>".encode("utf-8"), "text/html; charset=utf-8")
                    return
                if owner.reject_after_write:
                    # O POST chegou (a candidatura saiu) e o provedor recusou.
                    self._send(200, render_rejected().encode("utf-8"), "text/html; charset=utf-8")
                    return
                self._send(200, render_confirmation().encode("utf-8"), "text/html; charset=utf-8")

            def handle_one_request(self) -> None:
                try:
                    super().handle_one_request()
                except ConnectionResetError:
                    self.close_connection = True

            def log_message(self, *_args: object) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_args: object) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2)
        self._server = None
        self._thread = None


def resume_pdf(path) -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    with path.open("wb") as handle:
        writer.write(handle)
    return path.read_bytes()


__all__ = [
    "CHALLENGE_MARKER",
    "ChallengeCapableATS",
    "JOB_ID",
    "REQUIRED_FIELDS",
    "MultipartPart",
    "ReceivedSubmission",
    "resume_pdf",
]
