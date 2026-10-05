# specs/05_vertical_saas_web/verification/PARSERS.py
"""
Parsers for Verification Gate Outputs.
Extracts: Structured Issues, File Paths, Line Numbers for Fixer Agent.
"""

import re
import json
from typing: List, Dict, Any
from kernel.state import VerificationGateResult, VerificationGateStatus

class SaaSGateParser:
    """Parse stdout/stderr from SaaS Web Gates into structured issues."""

    @staticmethod
    def parse_eslint(result: VerificationGateResult) -> VerificationGateResult:
        """ESLint JSON Output (`eslint -f json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            issues = []
            files = set()
            for file_report in data:
                filepath = file_report.get("filePath", "")
                for msg in file_report.get("messages", []):
                    if msg.get("severity") == 2: # Error
                        issues.append({
                            "file": filepath,
                            "line": msg.get("line"),
                            "column": msg.get("column"),
                            "rule": msg.get("ruleId"),
                            "message": msg.get("message"),
                            "severity": "error"
                        })
                        files.add(filepath)
            result.issues = issues
            result.files_to_fix = list(files)
        except json.JSONDecodeError:
            pass # Fallback to stderr parsing
        return result

    @staticmethod
    def parse_tsc(result: VerificationGateResult) -> VerificationGateResult:
        """TypeScript Compiler Output (`tsc --noEmit --pretty false`)."""
        # Regex for: file(line,col): error TSXXXX: message
        pattern = re.compile(r"^(.*?)\:(\d+)\,\d+\:\s+(error|warning)\s+TS(\d+)\:\s+(.*)$", re.MULTILINE)
        issues = []
        files = set()
        for match in pattern.finditer(result.stdout + "\n" + result.stderr):
            file, line, sev, code, msg = match.groups()
            if sev == "error":
                issues.append({
                    "file": file.strip(), "line": int(line), "code": f"TS{code}",
                    "message": msg.strip(), "severity": sev
                })
                files.add(file.strip())
        result.issues = issues
        result.files_to_fix = list(files)
        return result

    @staticmethod
    def parse_vitest(result: VerificationGateResult) -> VerificationGateResult:
        """Vitest JSON Reporter (`vitest run --reporter=json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            issues = []
            files = set()
            for suite in data.get("testResults", []):
                for test in suite.get("tests", []):
                    if test.get("status") == "fail":
                        for err in test.get("errors", []):
                            # Stack trace parsing is complex, link to test file
                            issues.append({
                                "file": suite.get("name", "unknown"),
                                "test": test.get("name"),
                                "message": err.get("message", "")[:500],
                                "stack": err.get("stack", "")[:1000],
                                "severity": "error"
                            })
                            files.add(suite.get("name", ""))
            result.issues = issues
            result.files_to_fix = list(files)
        except: pass
        return result

    @staticmethod
    def parse_nextjs_build(result: VerificationGateResult) -> VerificationGateResult:
        """Next.js Build Output. Look for 'Error:' 'Type error:' 'Module not found'."""
        # Next.js output is complex. Best: Use `--json` flag if available (experimental) or parse stderr.
        # Simplified: Find file paths in error logs.
        files = set()
        issues = []
        # Common patterns
        patterns = [
            r"(.*?)\:(\d+)\:(\d+)\s+Type error:",
            r"Module not found: Can't resolve '(.+?)' in '(.+?)'",
            r"Failed to compile\.\s+(.*?)\n",
        ]
        text = result.stdout + "\n" + result.stderr
        for pat in patterns:
            for m in re.finditer(pat, text):
                # Extract file/line heuristic
                pass # Implementation detail
        result.files_to_fix = list(files)
        return result

    @staticmethod
    def parse_playwright(result: VerificationGateResult) -> VerificationGateResult:
        """Playwright JSON Output (`--reporter=json`)."""
        if not result.stdout: return result
        try:
            # Playwright outputs one JSON per line
            for line in result.stdout.strip().split('\n'):
                data = json.loads(line)
                if data.get("type") == "test" and data.get("result", {}).get("status") == "failed":
                    result.files_to_fix.add(data.get("location", {}).get("file", ""))
        except: pass
        return result

    @staticmethod
    def parse_semgrep(result: VerificationGateResult) -> VerificationGateResult:
        """Semgrep SARIF or JSON (`--json`)."""
        if not result.stdout: return result
        try:
            data = json.loads(result.stdout)
            for res in data.get("results", []):
                if res.get("extra", {}).get("severity") == "ERROR":
                    result.files_to_fix.add(res.get("path"))
                    result.issues.append({
                        "file": res.get("path"),
                        "line": res.get("start", {}).get("line"),
                        "rule": res.get("check_id"),
                        "message": res.get("extra", {}).get("message"),
                    })
        except: pass
        return result

    @staticmethod
    def parse_prisma(result: VerificationGateResult) -> VerificationGateResult:
        """Prisma Validate/Generate Output."""
        # Prisma errors usually point to schema.prisma line
        pattern = re.compile(r"Error:.*?schema\.prisma.*?line (\d+)")
        for m in pattern.finditer(result.stderr):
            result.files_to_fix.add("prisma/schema.prisma")
            result.issues.append({"file": "prisma/schema.prisma", "line": int(m.group(1)), "message": m.group(0)})
        return result

    # Dispatcher
    PARSERS = {
        "lint_ts": parse_eslint,
        "typecheck_ts": parse_tsc,
        "test_unit": parse_vitest,
        "test_contract": parse_vitest,
        "build_nextjs": parse_nextjs_build,
        "test_e2e": parse_playwright,
        "security_audit": parse_semgrep,
        "prisma_validate": parse_prisma,
        "prisma_generate": parse_prisma,
    }

    def parse(self, gate_id: str, result: VerificationGateResult) -> VerificationGateResult:
        parser = self.PARSERS.get(gate_id)
        if parser:
            return parser(self, result) # Static method call hack
        return result
