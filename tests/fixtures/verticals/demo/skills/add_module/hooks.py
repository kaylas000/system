from kernel.skills import HookContext
from kernel.skills.hooks import insert_after_marker, read_file
from kernel.state import FileChange, VerificationGateResult, VerificationGateStatus


async def post_render(ctx: HookContext, changes: list[FileChange]) -> list[FileChange]:
    main = await read_file(ctx, "src/main.txt") or ""
    line = f"uses {ctx.inputs['name']}"
    content = insert_after_marker(main, "main of", line)
    return [*changes, FileChange(path="src/main.txt", content=content, action="update")]


async def validate(ctx: HookContext) -> list[VerificationGateResult]:
    main = await read_file(ctx, "src/main.txt") or ""
    ok = f"uses {ctx.inputs['name']}" in main
    return [
        VerificationGateResult(
            gate_id="registered",
            name="module registered",
            status=VerificationGateStatus.PASSED if ok else VerificationGateStatus.FAILED,
            command="(hook)",
            exit_code=0 if ok else 1,
            stderr="" if ok else "module not registered in src/main.txt",
            duration_ms=0,
        )
    ]
