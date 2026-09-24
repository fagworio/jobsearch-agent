"""ATS controlado: formulário real, POST real, confirmação real (JSA-E2E-001).

O formulário é declarado UMA vez, em `FORM_FIELDS`, e a mesma declaração gera o
HTML e a validação server-side. Se o agente deixar de preencher um campo
obrigatório, o servidor recusa — e é isso que torna "required fields =
completos" uma afirmação do servidor, não do próprio agente.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import re
import threading
from typing import Iterable

JOB_ID = "e2e-001"


@dataclass(frozen=True)
class FieldSpec:
    name: str  # nome de wire, ex.: job_application[first_name]
    label: str
    kind: str = "text"  # text | email | tel | url | textarea | select | radio | checkbox | file
    required: bool = True
    options: tuple[str, ...] = ()

    @property
    def control_id(self) -> str:
        match = re.search(r"\[([^\]]+)\]", self.name)
        return match.group(1) if match else self.name


#: O formulário do ATS controlado: os casos reais que o roadmap lista.
FORM_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec("job_application[first_name]", "First name"),
    FieldSpec("job_application[last_name]", "Last name"),
    FieldSpec("job_application[email]", "Email", kind="email"),
    FieldSpec("job_application[phone]", "Phone", kind="tel"),
    FieldSpec("job_application[location]", "Location (city)"),
    FieldSpec("job_application[linkedin]", "LinkedIn profile", kind="url", required=False),
    FieldSpec("job_application[github]", "GitHub profile", kind="url", required=False),
    FieldSpec("job_application[experience_years]", "Years of experience with WordPress", kind="select", options=("1", "2", "3", "4", "5", "6", "7", "8", "9", "10+")),
    FieldSpec("job_application[salary_expectation]", "Salary expectation"),
    FieldSpec("job_application[authorized_to_work_in_brazil]", "Are you legally authorized to work in Brazil?", kind="radio", options=("Yes", "No")),
    FieldSpec("job_application[requires_sponsorship]", "Will you require sponsorship?", kind="radio", options=("Yes", "No")),
    FieldSpec("job_application[source]", "How did you hear about this job?", kind="select", options=("LinkedIn", "Referral", "Other")),
    FieldSpec("job_application[why_this_role]", "Why are you interested in this role?", kind="textarea"),
    FieldSpec("job_application[consent]", "I agree to the processing of my personal data", kind="checkbox"),
    FieldSpec("job_application[resume]", "Resume", kind="file"),
)

REQUIRED_FIELDS: tuple[str, ...] = tuple(spec.name for spec in FORM_FIELDS if spec.required)


@dataclass(frozen=True)
class MultipartPart:
    name: str
    filename: str
    content_type: str
    payload: bytes

    @property
    def is_file(self) -> bool:
        return bool(self.filename)


@dataclass(frozen=True)
class ReceivedSubmission:
    """O que o servidor REALMENTE recebeu. É daqui que saem as asserções."""

    path: str
    content_type: str
    body: bytes
    parts: tuple[MultipartPart, ...] = ()

    @property
    def is_multipart(self) -> bool:
        return "multipart/form-data" in self.content_type.casefold()

    def fields(self) -> dict[str, str]:
        return {
            part.name: part.payload.decode("utf-8", "replace")
            for part in self.parts
            if not part.is_file
        }

    def files(self) -> dict[str, MultipartPart]:
        return {part.name: part for part in self.parts if part.is_file}

    def field(self, name: str, default: str = "") -> str:
        return self.fields().get(name, default)

    def missing_required(self) -> list[str]:
        present = self.fields()
        files = self.files()
        missing = [name for name in REQUIRED_FIELDS if name not in files and not present.get(name, "").strip()]
        return missing

    @property
    def resume(self) -> MultipartPart | None:
        for name, part in self.files().items():
            if "resume" in name:
                return part
        return None


def parse_multipart(content_type: str, body: bytes) -> tuple[MultipartPart, ...]:
    """Parser mínimo e determinístico de `multipart/form-data`."""
    match = re.search(r'boundary="?([^";]+)"?', content_type)
    if not match:
        return ()
    boundary = b"--" + match.group(1).encode("ascii")
    parts: list[MultipartPart] = []
    for chunk in body.split(boundary):
        if not chunk.strip() or chunk.strip() == b"--":
            continue
        head, _, payload = chunk.partition(b"\r\n\r\n")
        if not _:
            continue
        headers = head.decode("utf-8", "replace")
        name = _header_value(headers, "name")
        if not name:
            continue
        # Remove EXATAMENTE o CRLF do framing, nunca bytes do arquivo: um
        # `rstrip` levaria junto o newline final do PDF e o SHA recebido
        # deixaria de bater com o enviado — que e justamente o que se quer medir.
        if payload.endswith(b"\r\n"):
            payload = payload[:-2]
        parts.append(
            MultipartPart(
                name=name,
                filename=_header_value(headers, "filename"),
                content_type=_part_content_type(headers),
                payload=payload,
            )
        )
    return tuple(parts)


def _header_value(headers: str, key: str) -> str:
    match = re.search(rf'{key}="([^"]*)"', headers)
    return match.group(1) if match else ""


def _part_content_type(headers: str) -> str:
    match = re.search(r"Content-Type:\s*([^\r\n]+)", headers, re.IGNORECASE)
    return match.group(1).strip() if match else ""


def render_control(spec: FieldSpec) -> str:
    control_id = spec.control_id
    required = " required" if spec.required else ""
    label = f'<label for="{control_id}">{spec.label}</label>'
    if spec.kind == "textarea":
        return f'{label}<textarea id="{control_id}" name="{spec.name}"{required}></textarea>'
    if spec.kind == "select":
        options = "".join(f'<option value="{value}">{value}</option>' for value in spec.options)
        return f'{label}<select id="{control_id}" name="{spec.name}"{required}>{options}</select>'
    if spec.kind == "radio":
        radios = "".join(
            f'<label for="{control_id}-{index}">{value}'
            f'<input id="{control_id}-{index}" name="{spec.name}" type="radio" value="{value}"{required}></label>'
            for index, value in enumerate(spec.options)
        )
        return f'<fieldset><legend>{spec.label}</legend>{radios}</fieldset>'
    if spec.kind == "checkbox":
        return f'<label for="{control_id}">{spec.label}</label><input id="{control_id}" name="{spec.name}" type="checkbox" value="Yes"{required}>'
    accept = ' accept="application/pdf"' if spec.kind == "file" else ""
    return f'{label}<input id="{control_id}" name="{spec.name}" type="{spec.kind}"{accept}{required}>'


def render_form(action: str) -> str:
    controls = "\n      ".join(render_control(spec) for spec in FORM_FIELDS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Apply — WordPress Developer</title></head>
<body>
  <h1>WordPress Developer</h1>
  <form id="application_form" data-provider="greenhouse" action="{action}" method="post" enctype="multipart/form-data">
      {controls}
      <button type="submit">Submit application</button>
  </form>
</body></html>"""


class ControlledATS:
    """Servidor de teste. `submissions` é o registro do que chegou de verdade."""

    def __init__(self, *, job_id: str = JOB_ID, redirect_after_post: bool = True) -> None:
        self.job_id = job_id
        self.redirect_after_post = redirect_after_post
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
    def job_path(self) -> str:
        return f"/jobs/{self.job_id}"

    @property
    def apply_path(self) -> str:
        return f"/jobs/{self.job_id}/apply"

    @property
    def confirmation_path(self) -> str:
        return f"/jobs/{self.job_id}/confirmation"

    def url(self, path: str) -> str:
        return self.origin + path

    @property
    def apply_url(self) -> str:
        return self.url(self.apply_path)

    # -- ciclo de vida -------------------------------------------------------

    def __enter__(self) -> "ControlledATS":
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
                if self.path == owner.job_path:
                    body = (
                        f'<!doctype html><html><body><h1>WordPress Developer</h1>'
                        f'<a href="{owner.apply_path}">Apply</a></body></html>'
                    ).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                    return
                if self.path == owner.apply_path:
                    self._send(200, render_form(owner.apply_path).encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path == owner.confirmation_path:
                    body = b"<!doctype html><html><body><h1>Thank you for applying</h1><p>We received your application.</p></body></html>"
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
                    parts=parse_multipart(content_type, body) if "multipart/form-data" in content_type.casefold() else (),
                )
                owner.submissions.append(submission)
                missing = submission.missing_required()
                resume = submission.resume
                if missing or resume is None or not resume.payload.startswith(b"%PDF-"):
                    detail = ", ".join(missing) or ("resume" if resume is None else "resume is not a PDF")
                    error = f"<h1>Application rejected</h1><p>Missing or invalid: {detail}</p>".encode("utf-8")
                    self._send(422, error, "text/html; charset=utf-8")
                    return
                if owner.redirect_after_post:
                    self._send(303, b"", "text/plain; charset=utf-8", {"Location": owner.confirmation_path})
                    return
                body_html = b"<h1>Thank you for applying</h1><p>We received your application.</p>"
                self._send(200, body_html, "text/html; charset=utf-8")

            def handle_one_request(self) -> None:
                # O browser fecha conexoes keep-alive; sem isto o servidor imprime
                # um traceback de ConnectionResetError no meio do teste.
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


def required_field_names() -> Iterable[str]:
    return REQUIRED_FIELDS
