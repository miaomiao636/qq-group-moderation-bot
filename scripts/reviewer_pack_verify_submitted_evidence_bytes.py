# ruff: noqa: E402, I001, F401, F811, SIM105, S101
# Reviewer round-9 probe pack (dbd80a5), promoted VERBATIM into the repo suite.
# Only this header was added; no assertion and no logic was changed.
"""Independent read-only Git-object check, not the submitted identity verifier.

Reads only explicitly submitted image evidence objects; never opens a production DB.
"""

import argparse
import hashlib
import io
import json
import re
import subprocess
from collections import Counter

from PIL import Image


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True)
    parser.add_argument("--revision", default="e3f321c989e55fb3aeadd21796082341f16be908")
    parser.add_argument("--baseline", default="8299ce8fe71d66e8954437084afc99a7d535a492")
    args = parser.parse_args()

    def git(*parts):
        return subprocess.check_output(["git", "-C", args.repo, *parts])

    def blob(path):
        return git("show", f"{args.revision}:{path}")

    report_path = "docs/evidence/image-review/identity-20260919T152313Z.json"
    report = json.loads(blob(report_path))
    checks, failures, cache = [], [], {}
    for role, expected in (("allowed", "放行"), ("rejected", "撤回")):
        for item in report[role]:
            batch = item["batch"]
            prefix = "docs/evidence/image-review/" + batch
            if batch not in cache:
                entries = {}
                for line in blob(prefix + "/IMAGE_REVIEW.md").decode().splitlines():
                    if not re.match(r"^\| \d+ \|", line):
                        continue
                    cells = [cell.strip().strip("`") for cell in line.split("|")]
                    entries[str(int(cells[1]))] = {
                        "file": cells[3],
                        "sha": cells[5],
                        "dhash": cells[6],
                    }
                decisions = json.loads(blob(prefix + "/DECISIONS.json"))
                cache[batch] = (entries, decisions)
            entries, decisions = cache[batch]
            entry = entries.get(str(int(item["no"])))
            if entry is None:
                failures.append(
                    {
                        "role": role,
                        "batch": batch,
                        "no": item["no"],
                        "problem": "missing manifest row",
                    }
                )
                continue
            data = blob(prefix + "/" + item["file"])
            full_sha = hashlib.sha256(data).hexdigest()
            with Image.open(io.BytesIO(data)) as image:
                gray = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
                bits = 0
                for y in range(8):
                    for x in range(8):
                        bits = (bits << 1) | int(gray.getpixel((x, y)) > gray.getpixel((x + 1, y)))
                frames = getattr(image, "n_frames", 1)
            actual_hash = f"{bits:016x}"
            normalized_decisions = {str(int(k)): v for k, v in decisions["decisions"].items()}
            assertions = {
                "report_full_sha_equals_current_bytes": full_sha == item["file_sha256"],
                "manifest_prefix_matches_current_bytes": bool(entry["sha"])
                and full_sha.startswith(entry["sha"]),
                "file_name_matches": entry["file"] == item["file"],
                "dhash_matches_three_fields": actual_hash
                == item["file_dhash"]
                == item["phash"]
                == entry["dhash"],
                "decision_batch_matches": decisions.get("batch") == batch,
                "decision_matches_expected": normalized_decisions.get(str(int(item["no"])))
                == expected
                == item["decision"],
            }
            checks.append(
                {
                    "role": role,
                    "manifest_sha_length": len(entry["sha"]),
                    "frames": frames,
                    **assertions,
                }
            )
            if not all(assertions.values()):
                failures.append(
                    {"role": role, "batch": batch, "no": item["no"], "assertions": assertions}
                )
    changed = git(
        "diff",
        "--name-status",
        args.baseline,
        args.revision,
        "--",
        *("docs/evidence/image-review/" + batch for batch in sorted(cache)),
    ).decode()
    result = {
        "revision": args.revision,
        "baseline": args.baseline,
        "allowed_checked": sum(c["role"] == "allowed" for c in checks),
        "rejected_checked": sum(c["role"] == "rejected" for c in checks),
        "manifest_sha_lengths": dict(Counter(c["manifest_sha_length"] for c in checks)),
        "frame_counts": dict(Counter(c["frames"] for c in checks)),
        "failures": failures,
        "referenced_batch_changes_since_baseline": changed.splitlines(),
        "all_comparisons_passed": not failures and len(checks) == 69,
        "scope": "Current submitted Git bytes, manifest prefixes, dHash and stored DECISIONS only; not production DB or approval-time full-SHA/current-source proof.",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["all_comparisons_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
