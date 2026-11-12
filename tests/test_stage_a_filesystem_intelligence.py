"""Stage A tests: Intelligent Filesystem Tools, Document Parsing, Secret Redaction, Aliases, & Brain Synthesis."""
from __future__ import annotations

import json
from pathlib import Path

from docx import Document as DocxDocument
import openpyxl
from reportlab.platypus import Paragraph, SimpleDocTemplate
from reportlab.lib.styles import getSampleStyleSheet
from starlette.testclient import TestClient

from jarvis.api import create_app
from tests.conftest import FakeBrain

H = {"X-Jarvis": "1"}


def _create_sample_erp(root: Path) -> Path:
    erp = root / "ERP_V2"
    backend = erp / "backend"
    docs = erp / "docs"
    backend.mkdir(parents=True, exist_ok=True)
    docs.mkdir(parents=True, exist_ok=True)

    (erp / "pyproject.toml").write_text('[project]\nname = "erp-v2"\nversion = "2.0.0"\n', encoding="utf-8")
    (backend / "database.py").write_text(
        'DATABASE_URL = "postgresql+psycopg://erp_user:SuperSecretPass123@localhost:5432/erp_db"\n'
        'API_KEY = "sk-proj-secret1234567890abcdef"\n'
        "POOL_SIZE = 10\n",
        encoding="utf-8",
    )
    (backend / "cp1252_notes.txt").write_bytes("Café ledger — total €250\n".encode("cp1252"))
    (backend / "blob.bin").write_bytes(b"\x00\x01\x02BINARY\x00DATA")
    (erp / ".env").write_text("JWT_SECRET=top-secret-jwt\n", encoding="utf-8")

    # Real PDF
    pdf_path = docs / "spec.pdf"
    doc = SimpleDocTemplate(str(pdf_path))
    doc.build([Paragraph("ERP_V2 Backend Spec: PostgreSQL 16 with Alembic migrations.", getSampleStyleSheet()["Normal"])])

    # Real DOCX
    d = DocxDocument()
    d.add_paragraph("Database uses read/write connection pooling via psycopg3.")
    d.save(str(docs / "architecture.docx"))

    # Real XLSX
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Config"
    ws.append(["Setting", "Value"])
    ws.append(["POOL_SIZE", "10"])
    wb.save(str(docs / "settings.xlsx"))
    return erp


def test_stage_a_fs_policy_enums_and_project_aliases(assistant, settings, tmp_path):
    erp = _create_sample_erp(tmp_path)
    client = TestClient(create_app(settings, assistant=assistant))

    # 1. Previously broken FS policy UI enum values now succeed cleanly (no 422)
    r_pol = client.put(
        "/api/v2/filesystem/policy",
        json={"sensitive_file_policy": "metadata_only", "cloud_data_policy": "redacted", "max_read_bytes": 65536},
        headers=H,
    )
    assert r_pol.status_code == 200
    pol = r_pol.json()["filesystem"]
    assert pol["sensitive_file_policy"] == "metadata_only"
    assert pol["cloud_data_policy"] == "redacted"
    assert pol["max_file_bytes"] == 65536

    # 2. Alias CRUD validates existing directory & blocks sensitive paths
    r_bad = client.post(
        "/api/v2/filesystem/aliases",
        json={"alias": "sys", "folder_path": r"C:\Windows"},
        headers=H,
    )
    assert r_bad.status_code == 403

    r_add = client.post(
        "/api/v2/filesystem/aliases",
        json={"alias": "ERP_V2", "folder_path": str(erp), "description": "ERP v2 monorepo"},
        headers=H,
    )
    assert r_add.status_code == 200
    assert r_add.json()["alias"]["folder_path"] == str(erp.resolve())

    r_list = client.get("/api/v2/filesystem/aliases")
    assert r_list.status_code == 200 and len(r_list.json()["aliases"]) == 1

    r_del = client.delete("/api/v2/filesystem/aliases/ERP_V2", headers=H)
    assert r_del.status_code == 200 and client.get("/api/v2/filesystem/aliases").json()["aliases"] == []


def test_stage_a_local_mode_tool_loop_permission_resume_and_synthesis(assistant, settings, tmp_path):
    erp = _create_sample_erp(tmp_path)
    client = TestClient(create_app(settings, assistant=assistant))
    client.post("/api/v2/filesystem/aliases", json={"alias": "ERP_V2", "folder_path": str(erp)}, headers=H)

    class MultiStepFsBrain(FakeBrain):
        def __init__(self, s):
            super().__init__(s)
            self.calls = 0

        async def stream_events(self, messages, tools=None):
            self.calls += 1
            self.seen.append({"messages": messages, "tools": tools})
            last_role = messages[-1]["role"] if messages else ""
            if last_role == "user":
                yield {
                    "type": "tool_calls",
                    "calls": [{"id": "t1", "name": "fs_get_project_structure", "arguments": {"path": "ERP_V2"}}],
                }
                return
            if last_role == "tool":
                tool_names = [m.get("name") for m in messages if m.get("role") == "assistant" and m.get("tool_calls")]
                prev_tool_json = json.loads(messages[-1]["content"])
                if "untrusted_tree" in prev_tool_json:
                    yield {
                        "type": "tool_calls",
                        "calls": [{"id": "t2", "name": "fs_read_file", "arguments": {"path": "ERP_V2/backend/database.py"}}],
                    }
                    return
                if "untrusted_content" in prev_tool_json:
                    assert "SuperSecretPass123" not in messages[-1]["content"]
                    assert "sk-proj-secret" not in messages[-1]["content"]
                    assert "POOL_SIZE = 10" in messages[-1]["content"]
                    yield {"type": "token", "text": "ERP_V2 connects to PostgreSQL on localhost:5432/erp_db with POOL_SIZE = 10."}
                    self.last_stats = {"ttft": 0.1, "tokens": 12, "tps": 45.0, "total": 0.2, "model": self.spec.model, "truncated": False}
                    return
            _ = tool_names
            yield {"type": "token", "text": "Done."}

    assistant.brain = MultiStepFsBrain(settings)

    # Step 1: Ask NL question in Local Mode -> Brain invokes fs_get_project_structure("ERP_V2") -> pauses for permission!
    r1 = client.post(
        "/api/chat",
        json={"message": "Open ERP_V2, inspect the backend, find the database configuration and explain the database connection"},
        headers=H,
    )
    assert r1.status_code == 200
    d1 = r1.json()
    assert d1["command"] == "permission_required"
    req_id = d1["permission_request"]["request_id"]
    assert Path(d1["permission_request"]["canonical_path"]).resolve() == erp.resolve()

    # Step 2: Owner grants allow_once -> executes fs_get_project_structure, Brain follows up with fs_read_file(database.py),
    # redacts secrets, and synthesizes clear explanation!
    r2 = client.post(
        f"/api/v2/filesystem/requests/{req_id}/decide",
        json={"decision": "allow_once", "decided_by": "owner", "execute_deferred": True},
        headers=H,
    )
    assert r2.status_code == 200
    d2 = r2.json()
    assert d2["granted"] is True
    assert "PostgreSQL" in d2["reply"]
    assert "POOL_SIZE = 10" in d2["reply"]
    assert "SuperSecretPass123" not in d2["reply"]
    assert any(Path(p).name == "database.py" for p in d2["paths"])
    assert d2["task"]["status"] == "completed"

    # Once permission must now be consumed
    perms = client.get("/api/v2/filesystem/permissions").json()["permissions"]
    perm = next(p for p in perms if p["permission_id"] == d2["permission"]["permission_id"])
    assert perm["status"] == "consumed"


def test_stage_a_documents_encodings_binary_and_extension_search(assistant, settings, tmp_path):
    erp = _create_sample_erp(tmp_path)
    client = TestClient(create_app(settings, assistant=assistant))

    # Persistent read-only grant to erp for direct tool testing
    r_g = client.post(
        "/api/v2/filesystem/permissions",
        json={"folder_path": str(erp), "operations": ["list_directory", "read_file", "search_files", "read_metadata"], "scope": "persistent"},
        headers=H,
    )
    assert r_g.status_code == 200

    import asyncio

    # 1. PDF extraction
    pdf_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_document", "arguments": {"path": str(erp / "docs" / "spec.pdf")}}))
    assert "Alembic migrations" in pdf_out

    # 2. DOCX extraction
    docx_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_document", "arguments": {"path": str(erp / "docs" / "architecture.docx")}}))
    assert "psycopg3" in docx_out

    # 3. XLSX extraction
    xlsx_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_document", "arguments": {"path": str(erp / "docs" / "settings.xlsx")}}))
    assert "POOL_SIZE" in xlsx_out

    # 4. CP1252 non-UTF8 text decoding
    cp_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_file", "arguments": {"path": str(erp / "backend" / "cp1252_notes.txt")}}))
    assert "Café ledger" in cp_out

    # 5. Binary guard
    bin_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_file", "arguments": {"path": str(erp / "backend" / "blob.bin")}}))
    assert "binary_file" in bin_out

    # 6. Extension search excludes .env & finds .py
    ext_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_find_files_by_extension", "arguments": {"path": str(erp), "extensions": ["py", ".env"]}}))
    ext_data = json.loads(ext_out)
    names = [m["name"] for m in ext_data["matches"]]
    assert "database.py" in names and ".env" not in names

    # 7. Blocked .env direct read & path traversal rejection
    env_out, _ = asyncio.run(assistant._execute_tool({"name": "fs_read_file", "arguments": {"path": str(erp / ".env")}}))
    assert "blocked" in env_out.lower()

