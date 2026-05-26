#!/usr/bin/env python3
"""FIRE solver wrapper.

Two ways to use this module:

  1. As a library — call :func:`run` to reduce one integral and get back the
     parsed result dict (matching the schema of every other solver in
     ``solvers/``):

         from solvers.fire import run as fire_run
         result = fire_run.run(
             integral=(2, 2, 2, 2, 2),
             params={"d": 6599, "m1sq": 4356, "spkk": 47},
             topology="5D/bl2em",
             root_dir="/path/to/feynman-ibp-bench",
             threads=4,
         )

  2. As a CLI — exposes the same flags as the original ``cli/run_fire.py``,
     useful for ad-hoc one-off runs and debugging.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml

from .parse import parse_fire_output


# ── Public API ──────────────────────────────────────────────────────────────

def run(integral, params, topology, *, root_dir, fire_path=None,
        fire_docker=None, threads=4,
        output_base=None, keep_temp=False, log_stream=None):
    """Reduce a single integral with FIRE and return the parsed result dict.

    Parameters
    ----------
    integral : tuple[int, ...] | list[int] | str
        Target integral indices (length matches the topology's propagator
        count). String form ``"2,2,2,2,2"`` is also accepted.
    params : dict[str, int]
        Parameter assignments keyed by the topology's CLI parameter names
        (see ``topologies/<topo>/parameters.yaml``).
    topology : str
        Path under ``topologies/``, e.g. ``"5D/bl2em"``.
    root_dir : str | Path
        Repo root (the directory containing ``topologies/`` and ``config.yaml``).
    fire_path : str | Path, optional
        Override FIRE binary path. Defaults to ``solvers.fire`` in
        ``<root_dir>/config.yaml`` when that entry is a string.
    fire_docker : dict, optional
        Override docker invocation config. Keys: ``image`` (required),
        ``binary`` (default ``FIRE6p``), ``platform`` (default
        ``linux/amd64``), ``extra_args`` (list of additional ``docker run``
        flags). When set, FIRE runs inside ``docker run`` instead of as a
        host binary. Falls back to ``solvers.fire.docker`` in config.yaml.
    threads : int
        FIRE-internal thread count (template ``{{THREADS}}``).
    output_base : str | Path, optional
        Where the per-run output directory lands. Defaults to
        ``<root_dir>/outputs``. Must live under ``root_dir`` when using
        docker (the repo root is the bind mount).
    keep_temp : bool
        If True, leave FIRE's ``temp/`` database directory in place.
    log_stream : file, optional
        If given, FIRE's stdout/stderr is teed there in addition to the
        per-run log file. ``None`` (default) means log to file only.

    Returns
    -------
    dict
        The parsed FIRE result with an added ``topology_path`` field set to
        ``topology`` (so callers can keep records consistent with the
        ground-truth schema).
    """
    root_dir = Path(root_dir).resolve()
    topology = str(topology)
    integral_tuple = _coerce_integral(integral)
    integral_csv = ",".join(str(x) for x in integral_tuple)

    working_dir = root_dir / "topologies" / topology / "fire"
    topology_name = Path(topology).name

    config_template = working_dir / f"{topology_name}.config.template"
    integral_template = working_dir / f"{topology_name}.m.template"
    sbases_file = working_dir / f"{topology_name}.sbases"
    lbases_file = working_dir / f"{topology_name}.lbases"

    for required in (working_dir, config_template, integral_template,
                     sbases_file, lbases_file):
        if not required.exists():
            raise FileNotFoundError(f"FIRE setup file missing: {required}")

    invocation = _resolve_fire_invocation(fire_path, fire_docker, root_dir)

    output_base = (Path(output_base).resolve()
                   if output_base else root_dir / "outputs")
    output_base.mkdir(parents=True, exist_ok=True)
    if invocation["mode"] == "docker":
        try:
            output_base.relative_to(root_dir)
        except ValueError:
            raise ValueError(
                f"Docker mode requires output_base under root_dir "
                f"(only one bind mount). Got output_base={output_base}, "
                f"root_dir={root_dir}."
            )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    integral_id = integral_csv.replace(",", "_")
    output_dir = output_base / f"output_{timestamp}_{topology_name}_fire_{integral_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1) Render integral file
    integral_file = output_dir / f"{topology_name}_integral.m"
    integral_file.write_text(
        _render(integral_template.read_text(),
                {"INTEGRALS": _format_integrals_mathematica([integral_csv])})
    )

    # 2) Validate params against topology dict, render config
    param_dict = _load_parameters_dict(root_dir / "topologies" / topology)
    for key in param_dict:
        if key not in params:
            raise KeyError(
                f"Missing parameter {key!r} for topology {topology}. "
                f"Provided: {sorted(params)}; expected: {sorted(param_dict)}"
            )
    parameters_block = ",".join(
        f"{entry['fire']}->{params[key]}"
        for key, entry in param_dict.items()
    )

    config_file = output_dir / f"{topology_name}_reduction.config"
    output_table = output_dir / f"{topology_name}_reduction.tables"
    # Prefix with ./ so FIRE7's std::filesystem::create_directories() gets "."
    # instead of "" (empty paths throw Invalid argument).
    config_file.write_text(_render(config_template.read_text(), {
        "THREADS": threads,
        "PARAMETERS": parameters_block,
        "SBASES_PATH": os.path.relpath(sbases_file, output_dir),
        "LBASES_PATH": os.path.relpath(lbases_file, output_dir),
        "INTEGRALS_PATH": integral_file.name,
        "OUTPUT_PATH": "./" + output_table.name,
    }))

    # 3) Run FIRE (host binary or docker container)
    log_file = output_dir / f"{topology_name}_fire_log.txt"
    cmd = _build_fire_cmd(invocation, config_file.stem, output_dir, root_dir)
    with open(log_file, "w") as log:
        log.write(f"Command: {' '.join(cmd)}\n")
        log.write(f"Working directory: {output_dir}\n")
        log.write(f"Topology: {topology}\n")
        log.write(f"Parameters: {params}\n")
        log.write("=" * 70 + "\n\n")
        log.flush()
        proc = subprocess.Popen(
            cmd, cwd=output_dir,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            universal_newlines=True, bufsize=1,
        )
        for line in proc.stdout:
            log.write(line)
            log.flush()
            if log_stream is not None:
                log_stream.write(line)
        proc.wait()

    if proc.returncode != 0:
        raise RuntimeError(
            f"FIRE failed (exit {proc.returncode}) on {topology} integral "
            f"[{integral_csv}]; see {log_file}"
        )

    # 4) Optional cleanup
    temp_dir = output_dir / "temp"
    if temp_dir.exists() and not keep_temp:
        shutil.rmtree(temp_dir)

    # 5) Parse and tag with topology_path so the record matches GT schema
    result = parse_fire_output(output_dir)
    result["topology_path"] = topology
    return result


# ── Helpers ─────────────────────────────────────────────────────────────────

def _coerce_integral(integral):
    if isinstance(integral, str):
        return tuple(int(x) for x in integral.split(","))
    return tuple(int(x) for x in integral)


def _resolve_fire_invocation(fire_path, fire_docker, root_dir):
    """Return how to invoke FIRE: either {mode: binary, path} or
    {mode: docker, image, binary, platform, extra_args}.

    Resolution order:
      1. Explicit ``fire_docker=`` kwarg wins.
      2. Explicit ``fire_path=`` kwarg.
      3. ``solvers.fire`` in config.yaml — either a string (binary path) or a
         dict with a ``docker:`` block.
    """
    if fire_docker is not None:
        return _normalize_docker_cfg(fire_docker)
    if fire_path:
        p = Path(fire_path)
        if not p.exists():
            raise FileNotFoundError(f"FIRE executable not found: {p}")
        return {"mode": "binary", "path": p}

    config_file = Path(root_dir) / "config.yaml"
    if not config_file.exists():
        raise FileNotFoundError(
            f"Neither fire_path nor fire_docker given and {config_file} not "
            "found (copy config.example.yaml to config.yaml)"
        )
    with open(config_file) as f:
        cfg = yaml.safe_load(f) or {}
    val = (cfg.get("solvers") or {}).get("fire")
    if not val:
        raise KeyError(f"solvers.fire not set in {config_file}")

    if isinstance(val, dict):
        if "docker" in val:
            return _normalize_docker_cfg(val["docker"])
        if "path" in val:
            p = Path(val["path"])
            if not p.exists():
                raise FileNotFoundError(f"FIRE executable not found: {p}")
            return {"mode": "binary", "path": p}
        raise ValueError(
            f"solvers.fire in {config_file} must be a binary path string, "
            "or a dict with a 'docker:' or 'path:' key."
        )

    p = Path(val)
    if not p.exists():
        raise FileNotFoundError(f"FIRE executable not found: {p}")
    return {"mode": "binary", "path": p}


def _normalize_docker_cfg(d):
    if not isinstance(d, dict):
        raise TypeError(f"docker config must be a dict, got {type(d).__name__}")
    image = d.get("image")
    if not image:
        raise KeyError("docker config requires an 'image' key")
    extra_args = d.get("extra_args") or []
    if isinstance(extra_args, str):
        extra_args = extra_args.split()
    platform = d.get("platform")
    return {
        "mode": "docker",
        "image": str(image),
        "binary": str(d.get("binary") or "FIRE6p"),
        "platform": str(platform) if platform else None,
        "extra_args": [str(x) for x in extra_args],
    }


def _build_fire_cmd(invocation, config_stem, output_dir, root_dir):
    # FIRE7p takes ``-prime N`` on the CLI; FIRE6p reads ``#prime N`` from the
    # config and rejects the flag. The config template already sets ``#prime 0``
    # for both, so the CLI flag is only needed for FIRE7p (host-binary mode).
    if invocation["mode"] == "binary":
        return [str(invocation["path"]), "-prime", "0", "-c", config_stem]

    output_dir = Path(output_dir).resolve()
    root_dir = Path(root_dir).resolve()
    rel = output_dir.relative_to(root_dir)  # ValueError caught upstream
    container_workdir = f"/work/{rel.as_posix()}"

    cmd = ["docker", "run", "--rm"]
    if invocation["platform"]:
        cmd.extend(["--platform", invocation["platform"]])
    cmd.extend([
        "-v", f"{root_dir}:/work",
        "-w", container_workdir,
        "--user", f"{os.getuid()}:{os.getgid()}",
    ])
    cmd.extend(invocation["extra_args"])
    cmd.extend([
        invocation["image"],
        invocation["binary"], "-c", config_stem,
    ])
    return cmd


def _load_parameters_dict(topology_dir):
    params_file = Path(topology_dir) / "parameters.yaml"
    if not params_file.exists():
        raise FileNotFoundError(f"parameters.yaml not found: {params_file}")
    with open(params_file) as f:
        data = yaml.safe_load(f) or {}
    pd = data.get("parameters") or {}
    if not pd:
        raise ValueError(f"No parameters block in {params_file}")
    return pd


def _format_integrals_mathematica(integrals):
    lines = []
    for i, integral in enumerate(integrals):
        sep = "" if i == len(integrals) - 1 else ","
        lines.append(f"{{0,{{{integral}}}}}{sep}")
    return "\n".join(lines)


def _render(template, replacements):
    out = template
    for key, value in replacements.items():
        out = out.replace(f"{{{{{key}}}}}", str(value))
    return out


def _parse_params_arg(s):
    """CLI helper: ``d->6599,m1sq->4356`` -> dict."""
    out = {}
    for pair in s.split(","):
        if "->" in pair:
            k, v = pair.split("->", 1)
            out[k.strip()] = int(v.strip()) if v.strip().lstrip("-").isdigit() else v.strip()
    return out


# ── CLI ────────────────────────────────────────────────────────────────────

def _main():
    parser = argparse.ArgumentParser(
        description="Run FIRE on one or more target integrals.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--params", "-p", required=True,
                        help='Parameters, e.g. "d->6599,m1sq->4356,spkk->47"')
    parser.add_argument("--integrals", "-i", nargs="+", required=True,
                        help='One or more integrals, e.g. "2,2,2,2,2"')
    parser.add_argument("--topology", "-t", required=True,
                        help='Topology path, e.g. "5D/bl2em"')
    parser.add_argument("--root-dir", required=True,
                        help="Repo root (contains topologies/, config.yaml).")
    parser.add_argument("--fire-path", "-f", default=None,
                        help="FIRE binary path (overrides config.yaml).")
    parser.add_argument("--docker-image", default=None,
                        help="Run FIRE inside this docker image instead of a "
                             "host binary (overrides config.yaml).")
    parser.add_argument("--docker-binary", default="FIRE6p",
                        help="Binary name to invoke inside the docker image "
                             "(default: FIRE6p).")
    parser.add_argument("--docker-platform", default="linux/amd64",
                        help="Docker --platform value (default: linux/amd64).")
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--output-base", "-o", default=None)
    parser.add_argument("--keep-temp", action="store_true")
    parser.add_argument("--results-dir", default=None,
                        help="If given, also write parsed result JSON here.")
    args = parser.parse_args()

    params = _parse_params_arg(args.params)
    fire_docker = None
    if args.docker_image:
        fire_docker = {
            "image": args.docker_image,
            "binary": args.docker_binary,
            "platform": args.docker_platform,
        }
    for integral_csv in args.integrals:
        result = run(
            integral=integral_csv, params=params, topology=args.topology,
            root_dir=args.root_dir, fire_path=args.fire_path,
            fire_docker=fire_docker,
            threads=args.threads, output_base=args.output_base,
            keep_temp=args.keep_temp, log_stream=sys.stdout,
        )
        if args.results_dir:
            os.makedirs(args.results_dir, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            topo_name = Path(args.topology).name
            integral_id = integral_csv.replace(",", "_")
            out = Path(args.results_dir) / f"result_{ts}_{topo_name}_fire_{integral_id}.json"
            out.write_text(json.dumps(result, indent=2))
            print(f"Result: {out}")
        else:
            json.dump(result, sys.stdout, indent=2)
            print()


if __name__ == "__main__":
    _main()
