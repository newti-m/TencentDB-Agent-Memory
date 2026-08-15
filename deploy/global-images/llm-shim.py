#!/usr/bin/env python3
"""LLM shim for the TDAI memory stack: 172.17.0.1:11439 -> 127.0.0.1:11434.

Why it exists (2026-08-14 bulk-ingest debugging):
- qwen3.6 thinking output is split from the answer at an unstable boundary by
  Ollama; wiki-ingest FILE blocks randomly land in the hidden `reasoning`
  field, so the knowledge service sees FILE-less content and fails the source
  (383/396 failures). The shim merges reasoning back into content whenever the
  content half has no FILE blocks but the reasoning half does.
- Disabling reasoning entirely (reasoning_effort none) made the model skip the
  FILE protocol altogether - do not go that route.
- The hub never sets temperature, so the Modelfile default (1.0) applied to a
  strict-format task; the shim pins a saner default.

Logs metadata per call to /var/log/tdai-llm-shim.jsonl; captures full content
of responses that still end up FILE-less after merging.
"""
import json, http.server, urllib.request

UPSTREAM = "http://127.0.0.1:11434"
LOG = "/var/log/tdai-llm-shim.jsonl"
NL = chr(10)

class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        if self.path.endswith("/chat/completions"):
            try:
                d = json.loads(body)
                d.setdefault("temperature", 0.6)
                body = json.dumps(d).encode()
            except Exception:
                pass
        req = urllib.request.Request(UPSTREAM + self.path, body,
            {"Content-Type": self.headers.get("Content-Type", "application/json")})
        try:
            with urllib.request.urlopen(req, timeout=1200) as r:
                resp = r.read(); code = r.status
        except urllib.error.HTTPError as e:
            resp = e.read(); code = e.code

        merged = False
        try:
            import re as _re
            rd = json.loads(resp)
            msg = rd.get("choices", [{}])[0].get("message", {})
            c = msg.get("content") or ""
            # Near-miss repair: model sometimes writes "FILE path=wiki/x.md"
            # without the <<<...>>> wrapper. Rebuild proper blocks; only fires
            # when the standard parse would find nothing anyway.
            if "<<<FILE" not in c:
                marks = list(_re.finditer(r"^[ 	]*(?:<+)?FILE[ 	]+path[ 	]*=[ 	]*\"?([\w\-./]+)\"?[ 	]*(?:>+)?[ 	]*$", c, _re.M))
                if marks:
                    parts = []
                    for i, m in enumerate(marks):
                        body = c[m.end():marks[i+1].start() if i+1 < len(marks) else len(c)]
                        body = _re.sub(r"^[ 	]*(?:<+)?END(?:>+)?[ 	]*$", "", body, flags=_re.M)
                        parts.append((chr(60)*3) + chr(70)+chr(73)+chr(76)+chr(69) + chr(32) + "path=" + chr(34) + m.group(1) + chr(34) + (chr(62)*3) + body.rstrip() + chr(10) + (chr(60)*3) + "END" + (chr(62)*3))
                    msg["content"] = (chr(10)*2).join(parts)
                    msg["normalized"] = True
                    resp = json.dumps(rd).encode()
        except Exception:
            pass
        try:
            rd = json.loads(resp)
            msg = rd.get("choices", [{}])[0].get("message", {})
            c = msg.get("content") or ""
            rzn = msg.get("reasoning") or msg.get("reasoning_content") or ""
            if "<<<FILE" not in c and "<<<FILE" in rzn:
                msg["content"] = rzn + NL + c
                msg["reasoning"] = ""
                merged = True
                resp = json.dumps(rd).encode()
        except Exception:
            rd = None

        try:
            if rd is not None:
                msg = rd.get("choices", [{}])[0].get("message", {})
                c = msg.get("content") or ""
                meta = {"path": self.path, "code": code, "merged": merged,
                        "usage": rd.get("usage"),
                        "finish": rd.get("choices", [{}])[0].get("finish_reason"),
                        "content_chars": len(c),
                        "reasoning_chars": len(msg.get("reasoning") or "")}
                if "/chat/completions" in self.path and "<<<FILE" not in c:
                    meta["content_head"] = c[:500]
                    try:
                        rq = json.loads(body)
                        meta["req_user_head"] = (rq.get("messages") or [{}])[-1].get("content", "")[:300]
                    except Exception:
                        pass
            else:
                meta = {"path": self.path, "code": code, "raw_len": len(resp)}
            with open(LOG, "a") as f:
                f.write(json.dumps(meta) + NL)
        except OSError:
            pass

        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def do_GET(self):
        try:
            with urllib.request.urlopen(UPSTREAM + self.path, timeout=60) as r:
                resp = r.read(); code = r.status
        except urllib.error.HTTPError as e:
            resp = e.read(); code = e.code
        self.send_response(code)
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("172.17.0.1", 11439), H).serve_forever()
