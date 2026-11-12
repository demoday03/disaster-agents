"""화면(HTML) 안의 스크립트와 webapp/*.js에 문법 오류가 없는지 확인한다(node --check).
한 줄짜리 함수 중간에 주석을 넣는 것 같은 실수로 화면 전체가 멈추는 것을 막는다. node가 없으면 건너뛴다."""
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")


@unittest.skipUnless(NODE, "node가 없어 화면 스크립트 문법 검사를 건너뜀")
class TestPageScripts(unittest.TestCase):
    def check(self, code, where):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code)
        try:
            r = subprocess.run([NODE, "--check", f.name], capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{where}: {r.stderr[-400:]}")
        finally:
            Path(f.name).unlink(missing_ok=True)

    def test_inline_and_static_scripts(self):
        pages = sorted((ROOT / "webapp").glob("*.html"))
        self.assertTrue(pages)
        for page in pages:
            for n, code in enumerate(re.findall(r"<script>(.*?)</script>", page.read_text(encoding="utf-8"), flags=re.S)):
                self.check(code, f"{page.name} 스크립트 {n + 1}")
        for js in sorted((ROOT / "webapp").glob("*.js")):
            self.check(js.read_text(encoding="utf-8"), js.name)


if __name__ == "__main__":
    unittest.main()
