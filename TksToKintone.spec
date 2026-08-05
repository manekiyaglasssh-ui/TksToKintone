# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules


# This file may be evaluated from PyInstaller's --specpath directory.  Resolve
# repository inputs from an explicit override or by locating the repository
# marker above the spec, never from the process current directory by accident.
def _project_root() -> Path:
    override = os.environ.get('TKS_PROJECT_ROOT')
    if override:
        return Path(override).resolve()
    for candidate in (Path(__file__).resolve().parent, *Path(__file__).resolve().parents):
        if (candidate / 'app').is_dir() and (candidate / 'templates').is_dir():
            return candidate
    raise RuntimeError('Unable to locate TksToKintone project root')


PROJECT_ROOT = _project_root()
build_variant = os.environ.get('TKS_BUILD_VARIANT', 'normal').strip().lower()
if build_variant not in {'normal', 'no-update', 'no-helper', 'with-helper'}:
    build_variant = 'normal'

variant_dir = PROJECT_ROOT / 'build' / 'variant'
variant_dir.mkdir(parents=True, exist_ok=True)
variant_file = variant_dir / 'build_variant.txt'
variant_file.write_text(build_variant + '\n', encoding='utf-8')

extra_hiddenimports = []
excludes = []

# 受注No取得helper（別プロセス化）。frozen環境では本体exeを
# `TksToKintone.exe --tks-order-capture-helper` として呼び直すため、helperモジュールと
# その依存（UIA/COM取得の tks_cloud_capture）を確実に同梱する。lazy import なので明示する。
extra_hiddenimports += [
    'app.tks_order_capture_helper',
    'app.tks_cloud_capture',
    'app.captured_orders',
]

# TKS受注No取込の UI Automation 経路（app/tks_cloud_capture.py）は comtypes を使う。
# TKSCloud8 は WPF（HwndWrapper 配下）で Win32 子ウィンドウ列挙では子が空になるため、
# UIA 経路が必須。comtypes は実行時に comtypes.gen へ COM ラッパを生成する（凍結時は
# gen_dir=None でメモリ生成にしている）ため、サブモジュールを漏れなく同梱する。
try:
    extra_hiddenimports += collect_submodules('comtypes')
except Exception as exc:
    # Windows 以外のビルド環境では comtypes 未導入のことがある（その環境では UIA は使わない）。
    print(f"WARNING: comtypes submodules not collected: {exc}")
if build_variant == 'no-update':
    excludes.extend(['app.update_client', 'app.update_helper'])
else:
    extra_hiddenimports.append('app.update_client')

extra_binaries = []
extra_datas = [
    (str(PROJECT_ROOT / 'templates'), 'templates'),
    (str(PROJECT_ROOT / 'docs'), 'docs'),
    (str(PROJECT_ROOT / 'assets'), 'assets'),
    (str(variant_file), '.'),
]

# SumatraPDF本体はPyInstallerへ同梱しない。固定版の公式installerは
# build_exe.batで検証し、Inno SetupがセットアップEXE内へ直接同梱する。
a = Analysis(
    [str(PROJECT_ROOT / 'app' / 'main.py')],
    pathex=[str(PROJECT_ROOT)],
    binaries=extra_binaries,
    datas=extra_datas,
    hiddenimports=extra_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TksToKintone',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[str(PROJECT_ROOT / 'assets' / 'app_icon.ico')],
    version=str(PROJECT_ROOT / 'installer' / 'version_info.txt'),
)

collect_items = [exe, a.binaries, a.datas]
if build_variant == 'with-helper':
    # 更新補助プロセス（PowerShell を使わずに更新するための EXE）。
    # 本体と同じフォルダ（dist/TksToKintone/tks_update_helper.exe）へ同梱する。
    helper_a = Analysis(
        [str(PROJECT_ROOT / 'app' / 'update_helper.py')],
        pathex=[str(PROJECT_ROOT)],
        binaries=[],
        datas=[],
        hiddenimports=[],
        hookspath=[],
        hooksconfig={},
        runtime_hooks=[],
        excludes=[],
        noarchive=False,
        optimize=0,
    )
    helper_pyz = PYZ(helper_a.pure)
    helper_exe = EXE(
        helper_pyz,
        helper_a.scripts,
        [],
        exclude_binaries=True,
        name='tks_update_helper',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=True,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    collect_items.extend([helper_exe, helper_a.binaries, helper_a.datas])

coll = COLLECT(
    *collect_items,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='TksToKintone',
)
