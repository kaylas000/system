from kernel.state import VerificationGateResult


def parse_grep(result: VerificationGateResult) -> VerificationGateResult:
    files = sorted({line.split(":", 1)[0].removeprefix("./") for line in result.stdout.splitlines() if ":" in line})
    return result.model_copy(update={"files_to_fix": files, "issues": [{"file": f, "message": "TODO"} for f in files]})


PARSERS = {"parse_grep": parse_grep}
