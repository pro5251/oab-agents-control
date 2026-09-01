"""Read-only local readiness checks for a confirmed deployment.

The environment contract intentionally records only names and paths.  This
module checks the local facts that cannot be validated from YAML alone without
opening a cluster connection or exposing credential contents.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
from typing import Any, Callable, Mapping

from .environment import load_environment, load_environment_document


def collect_preflight(
    environment_file: str | Path,
    *,
    chart_path: str | Path,
    which: Callable[[str], str | None] = shutil.which,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return non-secret evidence for the local deployment prerequisites."""

    environment, load_errors = load_environment_document(environment_file)
    diagnostics = load_errors or load_environment(environment_file, require_ready=True)
    variables = dict(environ if environ is not None else os.environ)
    k3s = environment.get("k3s") if isinstance(environment, Mapping) else {}
    kubeconfigs = {
        "deployer": _kubeconfig_check(k3s, "deployer_kubeconfig_env", variables),
        "secret_materializer": _kubeconfig_check(k3s, "secret_materializer_kubeconfig_env", variables),
    }
    configured_paths = [check["resolved_path"] for check in kubeconfigs.values() if check["configured"]]
    distinct = len(configured_paths) == 2 and configured_paths[0] != configured_paths[1]
    for check in kubeconfigs.values():
        check.pop("resolved_path", None)
    chart = Path(chart_path).resolve(strict=False)
    chart_check = {
        "path": str(chart),
        "directory_exists": chart.is_dir(),
        "chart_metadata_exists": (chart / "Chart.yaml").is_file(),
    }
    tools = {name: bool(which(name)) for name in ("git", "helm", "kubectl")}
    contract_ready = not diagnostics
    kubeconfigs_ready = distinct and all(check["configured"] and check["file_exists"] for check in kubeconfigs.values())
    chart_ready = chart_check["directory_exists"] and chart_check["chart_metadata_exists"]
    return {
        "read_only": True,
        "ready": contract_ready and kubeconfigs_ready and chart_ready and all(tools.values()),
        "environment": str(Path(environment_file).resolve(strict=False)),
        "contract": {
            "ready": contract_ready,
            "diagnostics": [diagnostic.as_dict() for diagnostic in diagnostics],
        },
        "tools": tools,
        "kubeconfigs": {"distinct_paths": distinct, **kubeconfigs},
        "chart": chart_check,
    }


def _kubeconfig_check(k3s: Any, field: str, environ: Mapping[str, str]) -> dict[str, Any]:
    variable = k3s.get(field) if isinstance(k3s, Mapping) else None
    value = environ.get(variable) if isinstance(variable, str) else None
    path = Path(value).resolve(strict=False) if value else None
    return {
        "environment_variable": variable if isinstance(variable, str) else None,
        "configured": bool(value),
        "file_exists": bool(path and path.is_file()),
        # Internal-only until ``collect_preflight`` has compared both paths.
        "resolved_path": str(path) if path else None,
    }
