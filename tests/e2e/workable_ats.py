"""ATS controlado com FORMATO Workable: formulario e submit em enderecos DIFERENTES.

O defeito que este cenario existe para impedir e especifico e ja aconteceu: o
`ApplicationLoop` usava a **URL do formulario** como destino da candidatura. No
Workable isso e falso, e o proprio `providers.py` ja declarava os dois enderecos:

    formulario   /<account>/j/<shortcode>/apply
    submit       /api/v1/accounts/<account>/jobs/<shortcode>/applications

Sem esta separacao o preenchimento pode estar perfeito e a candidatura nunca
chega: a intent, a policy e a observacao apontam para a pagina, e o POST real
sai — ou nao sai — para outro lugar.

O markup segue o que o `WorkableAdapter` reconhece (nomes reais `firstname`,
`lastname`, `email`, `phone` e pergunta customizada com chave opaca `QA_<id>`).
"""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from tests.e2e.controlled_ats import MultipartPart, ReceivedSubmission, parse_multipart

ACCOUNT = "pavago"
SHORTCODE = "711D24E5DB"
JOB_ID = f"workable-{SHORTCODE}"

#: Nomes de wire do formulario, com os tipos que a Workable usa de verdade.
FORM_FIELDS: tuple[tuple[str, str, str, bool], ...] = (
    ("firstname", "First name", "text", True),
    ("lastname", "Last name", "text", True),
    ("email", "Email", "email", True),
    ("phone", "Phone", "tel", True),
    ("QA_1", "LinkedIn profile", "text", False),
)

REQUIRED_FIELDS: tuple[str, ...] = tuple(name for name, _label, _kind, required in FORM_FIELDS if required)


def render_control(name: str, label: str, kind: str, required: bool) -> str:
    flag = " required" if required else ""
    return f'<label for="{name}">{label}</label><input id="{name}" name="{name}" type="{kind}"{flag}>'


def render_form(submit_path: str) -> str:
    """Formulario dirigido por JS, como o SPA real.

    O `action` do form NAO e o endereco da candidatura: o SPA monta o multipart e
    envia por `fetch` para a API. Se o teste usasse um `<form action=...>` comum,
    o browser faria o POST para a propria pagina e o cenario mediria outra coisa.
    """
    controls = "\n      ".join(render_control(*spec) for spec in FORM_FIELDS)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Apply — WordPress Developer</title></head>
<body>
  <h1>WordPress Developer — {ACCOUNT}</h1>
  <form id="application_form" method="post" enctype="multipart/form-data">
      {controls}
      <label for="resume">Resume</label>
      <input id="resume" name="resume" type="file" accept="application/pdf" required>
      <button type="submit">Submit application</button>
  </form>
  <script>
    document.getElementById('application_form').addEventListener('submit', async (event) => {{
      event.preventDefault();
      const response = await fetch({submit_path!r}, {{ method: 'POST', body: new FormData(event.target) }});
      const text = await response.text();
      document.open();
      document.write(text);
      document.close();
    }});
  </script>
</body></html>"""


CONFIRMATION_HTML = (
    b"<!doctype html><html><body><h1>Thank you for applying</h1>"
    b"<p>We have received your application.</p></body></html>"
)


class WorkableATS:
    """Servidor de teste do formato Workable.

    `posts` registra TODO POST recebido, e `submissions` so o que chegou no
    endpoint de candidatura: e assim que se prova que a escrita foi para o
    endereco certo, e que a pagina do formulario nao recebeu escrita nenhuma.
    """

    def __init__(self) -> None:
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
        return f"/{ACCOUNT}/j/{SHORTCODE}"

    @property
    def apply_path(self) -> str:
        return f"/{ACCOUNT}/j/{SHORTCODE}/apply"

    @property
    def submit_path(self) -> str:
        return f"/api/v1/accounts/{ACCOUNT}/jobs/{SHORTCODE}/applications"

    def url(self, path: str) -> str:
        return self.origin + path

    @property
    def apply_url(self) -> str:
        return self.url(self.apply_path)

    @property
    def submit_url(self) -> str:
        return self.url(self.submit_path)

    # -- ciclo de vida -------------------------------------------------------

    def __enter__(self) -> "WorkableATS":
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
                if self.path in {owner.job_path, owner.apply_path}:
                    body = render_form(owner.submit_path).encode("utf-8")
                    self._send(200, body, "text/html; charset=utf-8")
                    return
                self._send(404, b"not found", "text/plain; charset=utf-8")

            def do_POST(self) -> None:  # noqa: N802 - nome do protocolo
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                content_type = self.headers.get("Content-Type", "")
                owner.posts.append(self.path)
                if self.path != owner.submit_path:
                    # A pagina do formulario NAO aceita POST: se o agente mandar a
                    # candidatura para ela, o teste tem de falhar alto.
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
                missing = [
                    name
                    for name in REQUIRED_FIELDS
                    if name not in submission.files() and not submission.field(name).strip()
                ]
                resume: MultipartPart | None = submission.resume
                if missing or resume is None or not resume.payload.startswith(b"%PDF-"):
                    detail = ", ".join(missing) or ("resume" if resume is None else "resume is not a PDF")
                    self._send(422, f"<h1>Application rejected</h1><p>{detail}</p>".encode("utf-8"), "text/html; charset=utf-8")
                    return
                self._send(200, CONFIRMATION_HTML, "text/html; charset=utf-8")

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
