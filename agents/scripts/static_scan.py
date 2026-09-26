import ast
import json
import sys
from pathlib import Path


def scan_file(path: Path):
    findings = []

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception as exc:
        findings.append({
            "file": str(path),
            "line": 1,
            "type": "parse_error",
            "evidence": str(exc),
        })
        return findings

    for node in ast.walk(tree):
        # Detect random module usage.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "random":
                    findings.append({
                        "file": str(path),
                        "line": node.lineno,
                        "type": "random_usage",
                        "evidence": "imports random module",
                    })

        elif isinstance(node, ast.ImportFrom):
            if node.module == "random":
                findings.append({
                    "file": str(path),
                    "line": node.lineno,
                    "type": "random_usage",
                    "evidence": "imports from random module",
                })

        # Detect time usage.
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "time":
                    findings.append({
                        "file": str(path),
                        "line": node.lineno,
                        "type": "time_usage",
                        "evidence": "imports time module",
                    })

        elif isinstance(node, ast.ImportFrom):
            if node.module == "time":
                findings.append({
                    "file": str(path),
                    "line": node.lineno,
                    "type": "time_usage",
                    "evidence": "imports from time module",
                })

        # Detect asyncio.create_task.
        elif isinstance(node, ast.Call):
            if (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "create_task"
            ):
                findings.append({
                    "file": str(path),
                    "line": node.lineno,
                    "type": "async_task",
                    "evidence": "calls asyncio.create_task",
                })

        # Detect module-level mutable containers.
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value

            if isinstance(value, (ast.Dict, ast.List, ast.Set)):
                if isinstance(node, ast.Assign):
                    names = [
                        target.id
                        for target in node.targets
                        if isinstance(target, ast.Name)
                    ]
                else:
                    names = (
                        [node.target.id]
                        if isinstance(node.target, ast.Name)
                        else []
                    )

                if names:
                    findings.append({
                        "file": str(path),
                        "line": node.lineno,
                        "type": "module_mutable_state",
                        "evidence": f"module-level mutable object: {', '.join(names)}",
                    })

    return findings


def main():
    if len(sys.argv) < 2:
        print("Usage: python static_scan.py <path>")
        sys.exit(1)

    target = Path(sys.argv[1])

    if target.is_file():
        files = [target]
    elif target.is_dir():
        files = sorted(target.rglob("*.py"))
    else:
        print(f"ERROR: Path does not exist: {target}")
        sys.exit(1)

    findings = []

    for path in files:
        findings.extend(scan_file(path))

    result = {
        "subagent": "static_scan",
        "target": str(target),
        "findings": findings,
        "finding_count": len(findings),
    }

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()