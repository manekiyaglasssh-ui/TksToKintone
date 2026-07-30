SumatraPDF.exe の配置について（ビルド担当者向け）
================================================

このディレクトリには、配布時に同梱する SumatraPDF のポータブル版 SumatraPDF.exe を
配置してください。ライセンス上の理由でバイナリはリポジトリにコミットしていません。

重要（ビルド前チェック）
------------------------
- SumatraPDF.exe はリポジトリ管理外です（Git にコミットされていません）。
- ビルド前に、Portable 版 SumatraPDF.exe をこのフォルダへ配置してください。
  未配置の場合、通常の build_exe.bat はビルドPC上で scripts\download_sumatra.py を実行して取得します。
- 配置先: tools\SumatraPDF\SumatraPDF.exe
- 取得後も配置できない場合、インストーラビルド（build_exe.bat / Inno Setup）は失敗します。
  build_exe.bat は Inno Setup を実行する前に再確認し、未配置なら分かりやすいエラーで停止します。
- 配布先PCで SumatraPDF をダウンロードする処理はありません。
- 開発用に同梱なしでビルドしたい場合のみ、build_exe.bat に
  --allow-missing-sumatra を付けてください（この場合インストーラ生成はスキップされ、
  リリース配布には使えません）。

手順
----
1. 公式サイトから「SumatraPDF ... 64-bit portable」をダウンロードする。
   https://www.sumatrapdfreader.org/download-free-pdf-viewer
2. ダウンロードした実行ファイルを、このフォルダに次の名前で置く。
   tools/SumatraPDF/SumatraPDF.exe
3. SOURCE-SumatraPDF.txt の「同梱バージョン」を、置いた exe のバージョンに更新する。
4. PyInstaller ビルド（TksToKintone.spec）とインストーラ（installer/tks-to-kintone.iss）は、
   この tools/SumatraPDF ディレクトリを同梱するよう設定済みです。

配置後のアプリ側の挙動
----------------------
- SumatraPDF.exe が同梱されていれば、印刷設定でパスを指定しなくても
  SumatraPDF 経由印刷を使用できます（sumatra_path_source = installed_bundled）。
- 新規環境では既定の印刷方式が自動的に SumatraPDF 経由になります
  （既存ユーザーの保存済み設定は尊重されます）。
