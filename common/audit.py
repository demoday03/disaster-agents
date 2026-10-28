"""호출 로그. 원문 대신 마스킹된 요약만 남긴다."""
import json, time
from .masking import mask


class AuditLog:
    def __init__(self, path=None):
        self.records, self.path = [], path

    def add(self, agent, input_summary="", output_summary="", approver=None,
            result="ok", error=None, ts=None):
        rec = {"ts": ts if ts is not None else time.time(), "agent": agent,
               "input": mask(str(input_summary))[:200],
               "output": mask(str(output_summary))[:200],
               "approver": approver, "result": result, "error": error}
        self.records.append(rec)
        if self.path:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec
