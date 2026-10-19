"""Tarefas em segundo plano do painel: buscas em lote e envio (com botão de pausar).

Cada tarefa roda numa thread com a própria conexão ao banco. O estado fica na tabela `estado`
(chave job_<tipo>), então o painel mostra o progresso mesmo depois de recarregar a página.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable

from . import db

log = logging.getLogger("prospector")

BUSCA = "busca"
ENVIO = "envio"
KINDS = (BUSCA, ENVIO)
MAX_LINES = 40


def _key(kind: str) -> str:
    return f"job_{kind}"


@dataclass
class _Running:
    thread: threading.Thread
    stop: threading.Event
    lines: deque = field(default_factory=lambda: deque(maxlen=MAX_LINES))


class JobManager:
    def __init__(self, app: Any, conn_factory: Callable[[], Any] | None = None):
        self.app = app
        self._running: dict[str, _Running] = {}
        self._lock = threading.Lock()
        self._conn_factory = conn_factory or (lambda: db.connect(app.settings.db_path))
        # se o painel caiu no meio de uma tarefa, marca como interrompida
        for kind in KINDS:
            st = self.status(kind)
            if st.get("status") == "rodando":
                self._save(kind, {**st, "status": "interrompido", "resumo": "O painel foi reiniciado durante a tarefa."})

    # ---------- estado ----------

    def status(self, kind: str) -> dict[str, Any]:
        raw = db.get_state(self.app.conn, _key(kind))
        try:
            return json.loads(raw) if raw else {"status": "parado"}
        except ValueError:
            return {"status": "parado"}

    def _save(self, kind: str, data: dict[str, Any], conn=None) -> None:
        db.set_state(conn or self.app.conn, _key(kind), json.dumps(data, ensure_ascii=False))

    def is_running(self, kind: str) -> bool:
        r = self._running.get(kind)
        return bool(r and r.thread.is_alive())

    # ---------- controle ----------

    def stop(self, kind: str) -> bool:
        r = self._running.get(kind)
        if not r or not r.thread.is_alive():
            return False
        r.stop.set()
        return True

    def _start(self, kind: str, target: Callable[[Any, _Running, Callable[[str], None]], str], titulo: str) -> bool:
        with self._lock:
            if self.is_running(kind):
                return False
            running = _Running(thread=None, stop=threading.Event())  # type: ignore[arg-type]
            started = db.iso(db.utcnow())

            def worker() -> None:
                conn = self._conn_factory()
                state = {"status": "rodando", "titulo": titulo, "inicio": started, "linhas": []}

                def out(line: str) -> None:
                    running.lines.append(f"{db.utcnow().astimezone(self.app.settings.tz):%H:%M} {line.strip()}")
                    self._save(kind, {**state, "linhas": list(running.lines)}, conn)

                try:
                    self._save(kind, state, conn)
                    resumo = target(conn, running, out)
                    final = "pausado" if running.stop.is_set() else "concluido"
                except Exception as exc:  # noqa: BLE001 - qualquer falha vira estado de erro visível no painel
                    log.exception("tarefa %s falhou", kind)
                    resumo, final = f"Erro: {exc}", "erro"
                    self.app.notify(f"tarefa '{titulo}' falhou", str(exc), urgent=True)
                self._save(kind, {**state, "status": final, "fim": db.iso(db.utcnow()), "resumo": resumo,
                                  "linhas": list(running.lines)}, conn)
                conn.close()

            running.thread = threading.Thread(target=worker, name=f"job-{kind}", daemon=True)
            self._running[kind] = running
            running.thread.start()
            return True

    # ---------- tarefas ----------

    def start_busca(self, limit: int | None = None) -> bool:
        from .app import App
        from .lote import BatchConfig, pending, run_batch

        def target(conn, running: _Running, out) -> str:
            worker_app = App(self.app.settings)
            worker_app.__dict__["conn"] = conn
            cfg = BatchConfig.load(self.app.settings.buscas_path)
            total = len(pending(conn, cfg))
            out(f"{total} busca(s) pendente(s); rodando até {limit or cfg.max_por_execucao} agora.")
            results = run_batch(conn, cfg, lambda q: worker_app.search(q, out=out), limit=limit, out=out,
                                should_stop=running.stop.is_set)
            ok = sum(1 for _, r in results if not r.startswith("erro"))
            novos = conn.execute("SELECT COUNT(*) FROM leads WHERE status = 'novo'").fetchone()[0]
            resumo = f"{ok}/{len(results)} buscas ok. {novos} leads novos prontos para a fila."
            worker_app.notify("buscas terminaram", resumo)
            return resumo

        return self._start(BUSCA, target, "Buscas em lote")

    def start_envio(self, aguardar: bool = False) -> bool:
        from .app import App
        from .sender import StoppableClock, run

        def target(conn, running: _Running, out) -> str:
            worker_app = App(self.app.settings)
            worker_app.__dict__["conn"] = conn
            s = worker_app.settings
            client = worker_app.whatsapp()
            report = run(conn, client, worker_app.rules, typing_min_s=s.typing_min_s, typing_max_s=s.typing_max_s,
                         dry_run=False, clock=StoppableClock(running.stop), wait_next_day=aguardar,
                         notify_enabled=s.notify, out=out, should_stop=running.stop.is_set)
            resumo = (f"{report.enviados} enviada(s), {report.pulados} pulada(s), {report.erros} erro(s). "
                      f"{report.motivo_fim or report.parado or ''}").strip()
            worker_app.notify("envio terminou", resumo, urgent=bool(report.parado))
            return resumo

        return self._start(ENVIO, target, "Envio dos aprovados")
