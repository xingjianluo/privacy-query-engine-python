"""用升级前版本（git 提交 a61b8d2）的真实代码生成一份旧版账本。

生成的账本就是"升级前服务实际写出来的 ledger.json"：
- 只有 principals / 扁平 queries 两个分区；
- queries 以 query_id 为全局键（旧版未按用户隔离）；
- 5 分量 cache_key、记录里没有 created_at；
- 查询结果只含加噪结果与抑制原因。

用法：
    uv run python tools/make_legacy_ledger.py
输出：
    tests/fixtures/legacy_ledger.json
"""
import json
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLD_REVISION = "a61b8d2"
OUTPUT = ROOT / "tests" / "fixtures" / "legacy_ledger.json"

GENERATOR = r"""
import json
import sys
from pathlib import Path

work = Path(sys.argv[1])

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger

settings = Settings(
    data_dir=work,
    default_budget_epsilon=10.0,
    max_query_epsilon=2.0,
    min_group_size=3,
)
ledger = Ledger(settings.ledger_path, settings.lock_path)
ds = load_dataset(settings.dataset_path)

# 用旧版真实授权逻辑建立两个老用户
ledger.grant("alice", 10.0)
ledger.grant("carol", 5.0)

def ask(principal, **kw):
    req = QueryRequest(principal=principal, **kw)
    resp = submit_query(req, ledger, ds, settings)
    print("submitted:", principal, kw["query_id"], "replayed=", resp.replayed)

# alice 的老查询：1) 纯 count；2) count 带小分组/空分组抑制；3) sum
ask(
    "alice",
    query_id="legacy-count-all",
    aggregation="count",
    dimension="city",
    epsilon=1.0,
)
ask(
    "alice",
    query_id="legacy-count-suppressed",
    aggregation="count",
    dimension="city",
    epsilon=1.0,
    groups=["Beijing", "Shanghai", "Shenzhen", "Nowhere"],
)
ask(
    "alice",
    query_id="legacy-sum-age",
    aggregation="sum",
    dimension="department",
    value_column="age",
    epsilon=1.5,
)

# carol 的老查询：与 alice 同编号 q-001（旧版全局共用编号空间）
ask(
    "carol",
    query_id="q-001",
    aggregation="count",
    dimension="city",
    epsilon=1.0,
)

raw = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
assert set(raw.keys()) == {"principals", "queries"}, raw.keys()
assert all("created_at" not in r for r in raw["queries"].values())
print(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True))
"""


def main() -> int:
    work_root = ROOT / ".legacy-build"
    if work_root.exists():
        shutil.rmtree(work_root)
    old_app = work_root / "app"
    old_app.mkdir(parents=True)
    for name in (
        "__init__.py",
        "config.py",
        "dataset.py",
        "errors.py",
        "models.py",
        "noise.py",
        "service.py",
        "storage.py",
    ):
        content = subprocess.run(
            ["git", "show", f"{OLD_REVISION}:app/{name}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        (old_app / name).write_text(content, encoding="utf-8")

    gen_path = work_root / "_gen.py"
    gen_path.write_text(GENERATOR, encoding="utf-8")
    proc = subprocess.run(
        [sys.executable, str(gen_path), str(work_root / "data")],
        cwd=work_root,
        check=True,
        capture_output=True,
        text=True,
    )
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)

    # 生成器最后一段 stdout 是账本 JSON
    marker = "{\n  \"principals\""
    start = proc.stdout.index(marker)
    ledger_text = proc.stdout[start:]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(ledger_text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)} ({len(json.loads(ledger_text)['queries'])} queries)")
    shutil.rmtree(work_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
