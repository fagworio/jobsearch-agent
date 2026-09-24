"""ATS controlado MULTI-STEP (JSA-LOOP-002).

Cinco telas, um formulario. Cada etapa substitui o DOM da anterior, como um SPA
real faz — e e isso que expoe o defeito que este ticket ataca: no fim, o
`ApplicationForm` contem apenas a ultima tela, enquanto o browser preencheu
quatro.

Duas decisoes de desenho importam:

1. **Avanco e local.** `Next`/`Continue`/`Review` sao `type="button"` e so mexem
   no DOM: nenhuma escrita intermediaria. O unico POST e o Submit final.
2. **A etapa de review e ADITIVA.** Ela nao limpa a tela anterior, porque o
   input de arquivo precisa sobreviver ate o POST — um `File` nao pode ser
   transportado para um `input hidden`. Ela acrescenta uma pergunta opcional, o
   que muda o fingerprint da superficie final sem exigir resposta nova.

O servidor valida TODOS os campos obrigatorios no POST final. Ele nao confia no
agente: se uma etapa nao foi preenchida, o POST chega incompleto e o servidor
recusa.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from typing import Any

from tests.e2e.controlled_ats import (
    FieldSpec,
    ReceivedSubmission,
    parse_multipart,
)

JOB_ID = "e2e-002"


@dataclass(frozen=True)
class StepSpec:
    name: str
    fields: tuple[FieldSpec, ...]
    button: str = "Next"
    #: True = nao limpa o DOM anterior (a etapa de review precisa do arquivo).
    additive: bool = False


CONTACT = StepSpec(
    "contact",
    (
        FieldSpec("job_application[first_name]", "First name"),
        FieldSpec("job_application[last_name]", "Last name"),
        FieldSpec("job_application[email]", "Email", kind="email"),
        FieldSpec("job_application[phone]", "Phone", kind="tel"),
    ),
)
EXPERIENCE = StepSpec(
    "experience",
    (
        FieldSpec("job_application[experience_years]", "Years of experience with WordPress", kind="select", options=("1", "3", "5", "7", "10+")),
        FieldSpec("job_application[salary_expectation]", "Salary expectation"),
        FieldSpec("job_application[authorized_to_work_in_brazil]", "Are you legally authorized to work in Brazil?", kind="radio", options=("Yes", "No")),
        FieldSpec("job_application[requires_sponsorship]", "Will you require sponsorship?", kind="radio", options=("Yes", "No")),
    ),
    button="Continue",
)
QUESTIONS = StepSpec(
    "questions",
    (
        FieldSpec("job_application[source]", "How did you hear about this job?", kind="select", options=("LinkedIn", "Referral", "Other")),
        FieldSpec("job_application[why_this_role]", "Why are you interested in this role?", kind="textarea"),
        FieldSpec("job_application[relevant_project]", "Tell us about a relevant project.", kind="textarea"),
    ),
)
ARTIFACTS = StepSpec(
    "artifacts",
    (
        FieldSpec("job_application[resume]", "Resume", kind="file"),
        FieldSpec("job_application[consent]", "I agree to the processing of my personal data", kind="checkbox"),
    ),
    button="Review",
)
REVIEW = StepSpec(
    "review",
    (FieldSpec("job_application[anything_else]", "Anything else we should know?", kind="textarea", required=False),),
    additive=True,
)

STEPS: dict[str, StepSpec] = {step.name: step for step in (CONTACT, EXPERIENCE, QUESTIONS, ARTIFACTS, REVIEW)}

#: Sequencias por cenario. `loop` repete a primeira tela; `extra` passa do
#: maximo de ciclos; `unknown` acrescenta pergunta factual sem fato.
SEQUENCES: dict[str, tuple[str, ...]] = {
    "happy": ("contact", "experience", "questions", "artifacts", "review"),
    "loop": ("contact", "experience", "contact", "questions", "artifacts", "review"),
    "extra": ("contact", "experience", "questions", "artifacts", "pad-1", "pad-2", "review"),
}

#: Campos extras por cenario (nome da etapa -> campos).
EXTRA_FIELDS: dict[str, dict[str, tuple[FieldSpec, ...]]] = {
    "unknown": {
        "questions": (FieldSpec("job_application[job_notice_period]", "Notice period"),),
    },
}

#: Telas de recheio para estourar `max_cycles` sem repetir fingerprint.
for _index in (1, 2):
    STEPS[f"pad-{_index}"] = StepSpec(
        f"pad-{_index}",
        # Opcionais de proposito: elas existem para DISTINGUIR fingerprints e
        # estourar o maximo de ciclos. Sendo obrigatorias sem resposta, o fluxo
        # pararia em NEEDS_ANSWER antes de chegar ao limite — que e outro teste.
        (FieldSpec(f"job_application[pad_{_index}]", f"Extra question {_index}", required=False),),
    )

def required_fields(scenario: str = "happy") -> tuple[str, ...]:
    """Obrigatorios do CENARIO: so as telas da sequencia dele.

    Exigir os campos de renegativo (`pad_1`, `job_notice_period`) no caminho
    feliz faria o servidor recusar um POST completo — foi o que aconteceu na
    primeira execucao, e a recusa veio como 422 com o POST ja entregue.
    """
    names: list[str] = []
    for step_name in SEQUENCES.get(scenario, SEQUENCES["happy"]):
        step = STEPS[step_name]
        names.extend(spec.name for spec in step.fields if spec.required)
        names.extend(
            spec.name
            for spec in EXTRA_FIELDS.get(scenario, {}).get(step_name, ())
            if spec.required
        )
    return tuple(dict.fromkeys(names))


def _step_payload(name: str, *, scenario: str) -> dict[str, Any]:
    step = STEPS[name]
    fields = list(step.fields) + list(EXTRA_FIELDS.get(scenario, {}).get(name, ()))
    return {
        "name": step.name,
        "button": step.button,
        "additive": step.additive,
        "fields": [
            {
                "name": item.name,
                "id": item.control_id,
                "label": item.label,
                "kind": item.kind,
                "required": item.required,
                "options": list(item.options),
            }
            for item in fields
        ],
    }


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Apply — Multi-step</title></head>
<body>
  <h1>WordPress Developer</h1>
  <progress id="progress" value="1" max="{total}"></progress>
  <form id="application_form" data-provider="greenhouse" action="{action}" method="post" enctype="multipart/form-data">
    <div id="carry"></div>
    <div id="step-container"></div>
    <button id="next" type="button">{first_button}</button>
    <button id="submit" type="submit" hidden>Submit application</button>
  </form>
  <script>
    const STEPS = {steps};
    const SEQUENCE = {sequence};
    let position = 0;
    const state = {{}};
    const container = document.getElementById('step-container');
    const carry = document.getElementById('carry');
    const nextButton = document.getElementById('next');
    const submitButton = document.getElementById('submit');
    const progress = document.getElementById('progress');

    function collect() {{
      container.querySelectorAll('[name]').forEach(function (element) {{
        if (element.type === 'file') return;
        if (element.type === 'checkbox') {{
          if (element.checked) state[element.name] = element.value; else delete state[element.name];
        }} else if (element.type === 'radio') {{
          if (element.checked) state[element.name] = element.value;
        }} else {{
          state[element.name] = element.value;
        }}
      }});
    }}

    function control(field) {{
      const wrapper = document.createElement('div');
      const label = document.createElement('label');
      label.setAttribute('for', field.id);
      label.textContent = field.label;
      wrapper.appendChild(label);
      let input;
      if (field.kind === 'textarea') {{
        input = document.createElement('textarea');
      }} else if (field.kind === 'select') {{
        input = document.createElement('select');
        field.options.forEach(function (option) {{
          const node = document.createElement('option');
          node.value = option; node.textContent = option;
          input.appendChild(node);
        }});
      }} else if (field.kind === 'radio') {{
        field.options.forEach(function (option, index) {{
          const optionId = field.id + '-' + index;
          const optionLabel = document.createElement('label');
          optionLabel.setAttribute('for', optionId);
          optionLabel.textContent = option;
          const radio = document.createElement('input');
          radio.type = 'radio'; radio.id = optionId; radio.name = field.name; radio.value = option;
          if (field.required) radio.required = true;
          wrapper.appendChild(optionLabel);
          wrapper.appendChild(radio);
        }});
        return wrapper;
      }} else {{
        input = document.createElement('input');
        input.type = field.kind;
        if (field.kind === 'file') input.accept = 'application/pdf';
      }}
      input.id = field.id; input.name = field.name;
      if (field.required) input.required = true;
      if (state[field.name] !== undefined) {{
        if (input.tagName === 'SELECT') input.value = state[field.name];
        else input.value = state[field.name];
      }}
      wrapper.appendChild(input);
      return wrapper;
    }}

    function render(index) {{
      collect();
      const step = STEPS[SEQUENCE[index]];
      if (!step.additive) container.innerHTML = '';
      const onScreen = new Set(Array.from(container.querySelectorAll('[name]')).map(function (e) {{ return e.name; }}));
      carry.innerHTML = '';
      STEPS && Object.values(STEPS).forEach(function (candidate) {{
        candidate.fields.forEach(function (field) {{
          if (onScreen.has(field.name) || state[field.name] === undefined) return;
          const hidden = document.createElement('input');
          hidden.type = 'hidden'; hidden.name = field.name; hidden.value = state[field.name];
          carry.appendChild(hidden);
        }});
      }});
      step.fields.forEach(function (field) {{ container.appendChild(control(field)); }});
      progress.value = index + 1;
      const last = index === SEQUENCE.length - 1;
      nextButton.hidden = last;
      submitButton.hidden = !last;
      if (!last) nextButton.textContent = step.button;
    }}

    nextButton.addEventListener('click', function () {{
      if (position + 1 < SEQUENCE.length) {{ position += 1; render(position); }}
    }});
    document.getElementById('application_form').addEventListener('submit', function () {{ collect(); }});
    render(0);
  </script>
</body></html>"""


class MultiStepATS:
    """Servidor de teste multi-step. `submissions` e o registro do POST final."""

    def __init__(self, *, scenario: str = "happy", job_id: str = JOB_ID) -> None:
        if scenario not in ("happy", "loop", "extra", "unknown"):
            raise ValueError(f"unknown scenario: {scenario}")
        self.scenario = scenario
        self.job_id = job_id
        self.sequence = SEQUENCES.get(scenario, SEQUENCES["happy"])
        self.submissions: list[ReceivedSubmission] = []
        self.posts: list[str] = []
        self.gets: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- URLs ------------------------------------------------------------------

    @property
    def origin(self) -> str:
        if self._server is None:
            raise RuntimeError("server is not running")
        return f"http://127.0.0.1:{self._server.server_port}"

    @property
    def apply_path(self) -> str:
        return f"/jobs/{self.job_id}/apply"

    @property
    def confirmation_path(self) -> str:
        return f"/jobs/{self.job_id}/confirmation"

    @property
    def apply_url(self) -> str:
        return self.origin + self.apply_path

    def page(self) -> str:
        steps = {name: _step_payload(name, scenario=self.scenario) for name in STEPS}
        return _PAGE.format(
            total=len(self.sequence),
            action=self.apply_path,
            first_button=STEPS[self.sequence[0]].button,
            steps=json.dumps(steps),
            sequence=json.dumps(list(self.sequence)),
        )

    # -- ciclo de vida ---------------------------------------------------------

    def __enter__(self) -> "MultiStepATS":
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
                    self._send(200, owner.page().encode("utf-8"), "text/html; charset=utf-8")
                    return
                if self.path == owner.confirmation_path:
                    self._send(200, b"<h1>Thank you for applying</h1><p>We received your application.</p>", "text/html; charset=utf-8")
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
                missing = [
                    name
                    for name in required_fields(owner.scenario)
                    if name not in submission.files() and not submission.field(name).strip()
                ]
                resume = submission.resume
                if missing or resume is None or not resume.payload.startswith(b"%PDF-"):
                    detail = ", ".join(missing) or ("resume" if resume is None else "resume is not a PDF")
                    self._send(422, f"<h1>Application rejected</h1><p>Missing or invalid: {detail}</p>".encode("utf-8"), "text/html; charset=utf-8")
                    return
                self._send(303, b"", "text/plain; charset=utf-8", {"Location": owner.confirmation_path})

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
