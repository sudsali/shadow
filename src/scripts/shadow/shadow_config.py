"""Loads .shadow.yml from the adopter repo's root.

Precedence (highest to lowest): env var > .shadow.yml > built-in default.

Schema (all fields optional except codebase.src_dir, which has a sensible
default of repo root if missing):

    codebase:
      src_dir: src/main/scala       # primary source root
      file_ext: .scala              # default ""
      test_dir: src/test/scala      # default: inferred via _infer_test_dir
      language: scala               # legacy issue-respond path only; default ""
    bot:
      name: shadow                  # marker comment + comment provenance
      escalate_label: needs-human
    models:
      investigator: <model-id>      # default: built-in investigator default
      critic: <model-id>            # default: matches investigator (must support tool use)
      reporter: <model-id>          # default: matches investigator

"""
import logging
import os
from pathlib import Path

logger = logging.getLogger("shadow")


def load(repo_root="."):
    """Path-shaped values escaping repo root are rejected so an upstream
    maintainer's misconfig can't grep /etc/passwd. Under pull_request_target
    the repo_root is the base-branch checkout — a malicious PR can't ship
    config to escalate bot privileges.
    """
    path = Path(repo_root) / ".shadow.yml"
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError:
        logger.warning("pyyaml not installed; .shadow.yml will be ignored")
        return {}
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to parse .shadow.yml: %s; using defaults", e)
        return {}
    return _sanitize(data)


_PATH_FIELDS = (("codebase", "src_dir"), ("codebase", "test_dir"))


def _sanitize(data):
    """Reject path-shaped values that escape repo root. Mutates `data`."""
    for keys in _PATH_FIELDS:
        val = data
        for k in keys[:-1]:
            if not isinstance(val, dict):
                val = None
                break
            val = val.get(k)
        if not isinstance(val, dict):
            continue
        last = keys[-1]
        raw = val.get(last)
        if not isinstance(raw, str) or not raw:
            continue
        if raw.startswith("/") or raw.startswith("\\") or (len(raw) >= 2 and raw[1] == ":"):
            logger.warning(".shadow.yml %s rejected: absolute path %r", ".".join(keys), raw)
            del val[last]
            continue
        if ".." in raw.replace("\\", "/").split("/"):
            logger.warning(".shadow.yml %s rejected: '..' segment in %r", ".".join(keys), raw)
            del val[last]
    return data


def get(cfg_dict, *keys, default=None):
    """Walk a nested dict; return `default` if any key is missing."""
    cur = cfg_dict
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur if cur is not None else default


def env_or(name, yaml_value, default):
    """env > yaml > default. Both string sides are stripped so YAML quoted
    whitespace doesn't leak through asymmetrically with the env path."""
    env = (os.getenv(name) or "").strip()
    if env:
        return env
    if isinstance(yaml_value, str):
        yaml_value = yaml_value.strip()
    if yaml_value not in (None, ""):
        return yaml_value
    return default
