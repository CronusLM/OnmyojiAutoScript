# This Python file uses the following encoding: utf-8
# @author runhey
# github https://github.com/runhey
"""
对比当前工作区与上游 ref 的 assets.py 资产定义集合。

背景：merge upstream/dev 时 assets.py 多为自动合并，若某资产定义丢失，
运行时会 AttributeError。本脚本保证上游每个资产都存在于合并结果中。

用法:
    python dev_tools/sync_assets_check.py                # 默认对比 upstream/dev
    python dev_tools/sync_assets_check.py --ref dev      # 对比其它 ref

退出码: 0=一致, 1=存在缺失资产
"""
import argparse
import glob
import re
import subprocess
import sys

ASSET_PATTERN = re.compile(r'^\s+([A-Z][A-Z0-9_]*)\s*=', re.M)


def asset_names(content: str) -> set:
    return set(ASSET_PATTERN.findall(content))


def git_show(ref: str, path: str) -> str:
    result = subprocess.run(['git', 'show', f'{ref}:{path}'], capture_output=True, text=True)
    if result.returncode != 0:
        print(f'ERROR: git show {ref}:{path} failed: {result.stderr.strip()}')
        sys.exit(1)
    return result.stdout


def main(ref: str) -> int:
    files = sorted(p for p in glob.glob('tasks/**/assets.py', recursive=True) if '__pycache__' not in p)
    if not files:
        print('WARNING: No assets.py found under tasks/, check working directory')
        return 1

    missing_total = 0
    for path in files:
        upstream = asset_names(git_show(ref, path))
        local = asset_names(open(path, encoding='utf-8').read())
        missing = upstream - local
        extra = local - upstream
        if missing or extra:
            print(f'--- {path} (vs {ref}) ---')
            if missing:
                print(f'  MISSING {len(missing)}: {sorted(missing)}')
                missing_total += len(missing)
            if extra:
                print(f'  extra (local only): {sorted(extra)}')
        else:
            print(f'{path}: OK ({len(local)} assets)')

    if missing_total:
        print(f'{missing_total} asset(s) missing vs {ref}, fix before commit')
        return 1
    print(f'All assets.py consistent with {ref}')
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Check assets.py definitions vs an upstream ref')
    parser.add_argument('--ref', default='upstream/dev', help='git ref to compare against (default: upstream/dev)')
    args = parser.parse_args()
    sys.exit(main(args.ref))
