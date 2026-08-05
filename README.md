# TksToKintone

 TKS OLAPから加工CSV・素板CSVを取得し、既存Excel VBA相当の加工を行って `outputTksToKintone.csv` を作成し、必要に応じてkintoneへ登録するWindows向けGUIアプリです。現在のバージョンネームは `1.6.1`、バージョンコードは `45` です。

ユーザー向けの操作手順は [docs/ユーザー向け簡易マニュアル.md](docs/ユーザー向け簡易マニュアル.md) を参照してください。

## バージョン 1.6.1 の変更点

- SumatraPDFをアプリへ同梱する方式から、Windowsへ独立インストールする方式へ変更しました。
- SumatraPDFが未導入の場合、新規インストール時と更新時のセットアップで自動導入します。
- 更新後にSumatraPDFが見つからず印刷できない問題を修正しました。
- インストール済みSumatraPDFを印刷時に自動検出します。

## インストール

1. Windows環境で `build_exe.bat` を実行して `dist\TksToKintone\TksToKintone.exe` を作成します。
2. Inno Setupで `installer\tks-to-kintone.iss` を開き、インストーラをビルドします。出力は常に `installer\tks-to-kintone-setup.exe` （バージョン番号なしの固定名）になります。過去にバージョン番号付きで生成した `tks-to-kintone-setup_x.y.z.exe` が残っている場合は混乱を避けるため削除してください。
3. インストール先は `C:\Program Files\Manekiya\TksToKintone`、設定とログは `C:\ProgramData\Manekiya\TksToKintone` です。

## 初回セットアップ手順

1. インストーラでアプリをインストールします。
2. スタートメニューまたはデスクトップショートカットから `TksToKintone` を起動します。
3. 初回起動時に `C:\ProgramData\Manekiya\TksToKintone` が作成され、`config.env` と `field_mapping.json` がsampleからコピーされます。
4. 初回は設定不足エラーで停止します。画面の `設定フォルダを開く`、またはエクスプローラーで `C:\ProgramData\Manekiya\TksToKintone` を開きます。
5. `config.env` を編集します。最初は `TKS_CLIENT_MODE=mock` でサンプルCSV確認を行い、その後 `http` へ切り替える流れを推奨します。
6. `field_mapping.json` を確認し、CSVヘッダー名とkintoneフィールドコードの対応が正しいことを確認します。
7. アプリを再起動します。画面上部に `TKS_CLIENT_MODE` とProgramDataフォルダが表示されます。
8. `mock` モードで変換確認後、`http` モードで `TKS接続テスト`、`OLAP取得テスト`、最後に `DRY_RUN=true` の登録前確認フローを確認します。
9. `DRY_RUN=false` にする前に、出力CSVとログを確認してください。kintone登録は `検索キー` を更新キーにして、既存があれば更新、なければ追加します。

## 設定

初回起動時に `C:\ProgramData\Manekiya\TksToKintone\config.env` と `field_mapping.json` がなければ、sampleからコピーして作成します。設定不足がある場合は起動時にエラーを表示します。

`config.env` には契約会社コード、TKS接続方式、TKS OLAP URL、kintoneドメイン、アプリID、APIトークン、作業フォルダ名、出荷区分候補、DRY_RUN初期値を設定します。OLAPログインIDとパスワードは初期状態では空白です。TKSログインに成功した後にユーザー設定へ保存し、次回起動時に復元します。APIトークンやパスワードはログに出しません。

`TKS_COMPANY_CODE` は `G29V-T8GL-9LYD` を使用します。古い契約会社コードではHTTP 200でも `ResponseData.Ｘ0=01` となり、ログイン失敗になります。

TKS接続方式は `TKS_CLIENT_MODE` で切り替えます。

- `mock`: `TKS_KAKOU_CSV_URL` と `TKS_SOBA_CSV_URL` に `file://` のCSVパスを指定し、TKS実接続なしでCSV加工以降を確認します。
- `http`: `TKS_BASE_URL` から `POST /c/ログイン認証` と `PUT /c/OLAPデータ` を実行します。

`http` では以下が必須です。

```env
TKS_BASE_URL=https://www.ap.tkscloud8.aga-sys.com
TKS_SCREEN_NAME=0
TKS_LOGIN_AUTH_KBN=2
TKS_TERMINAL_ID=
TKS_COMPUTER_NAME=
TKS_IP_ADDRESS=
TKS_KAKOU_REQUEST_TEMPLATE=docs/olap/kakou_request_template.json
TKS_SOBA_REQUEST_TEMPLATE=docs/olap/soba_request_template.json
```

加工抽出ロジックと素板抽出ロジックは、どちらも `OLAP出力レイアウト=1`、`OLAP対象データ=OLAP_T01-04 受注明細加工完成品データ2` を使います。これらの値はテンプレートJSON内に保持します。アプリは `docs/olap/kakou_request_template.json` と `docs/olap/soba_request_template.json` を読み込み、`R2List` 内の `フィールド論理名=受注No` の `OLAP値` だけを画面入力された伝票番号のカンマ区切りに差し替えます。その他の `R1List` / `R2List` 条件は変更しません。

`field_mapping.json` はCSVヘッダー名をkintoneフィールドコードへ対応付けるJSONです。例:

```json
{
  "受注No": "受注No",
  "硝/加工": "硝_加工",
  "仕上日": "仕上日",
  "出荷区分": "出荷区分"
}
```

## 画面の入力

契約会社コードは `config.env` から読み込み、画面では編集不可です。伝票番号は複数行入力に対応し、1行1伝票番号またはカンマ区切りで指定できます。仕上日は `yyyy-MM-dd` 形式、出荷区分は `SHUKKA_KBN_OPTIONS` の候補から選択します。

DRY_RUNがONの場合、TKS取得とCSV作成後に登録前確認画面を表示します。確認画面では伝票番号ごとに重複を除いた `受注No`、`仕上日`、`出荷区分` を確認し、仕上日と出荷区分を変更できます。変更した値は同じ受注Noの全レコードへ反映します。確認画面で `登録` を押した場合だけkintoneへ送信し、`登録キャンセル` を押した場合は送信しません。OFFの場合は確認画面を出さずにkintone REST API `PUT /k/v1/records.json` に `upsert=true` で100件単位登録します。`検索キー` が既存レコードにあれば更新し、なければ追加します。APIトークンには対象アプリのレコード追加・編集権限が必要です。

画面の `設定` からテーマカラー、Kintone接続先、デバッグ項目の表示/非表示、高度な設定、バージョン情報の確認ができます。テーマカラーは `システム`、`ライト`、`ダーク` から選択できます。Kintone接続先は `本番`、`テスト` から選択でき、初期値は `本番` です。`テスト` は `config.env` の現在の接続先、`本番` は本番用URL/アプリID/APIトークンを使用します。デバッグ表示の初期値はOFFです。機能選択画面の設定でデバッグ表示をONにすると、更新確認用KintoneのアプリIDとAPIトークンを代替接続先として設定できます。両方が空欄の場合は本番設定、両方が有効な場合だけデバッグ設定を更新確認とインストーラ取得に使用します。片方だけ、または不正値は保存エラーになり、有効なデバッグ接続先で通信に失敗しても本番へ自動切替しません。APIトークンは資格情報ストアへ保存し、ログには出力しません。デバッグ表示をOFFにすると保存値を残したまま本番設定を使用し、`TKS_CLIENT_MODE`、Kintone接続先、ProgramDataフォルダ、各フォルダを開くボタン、TKS接続テスト、OLAP取得テスト、ログ表示を非表示にします。

設定画面の `高度な設定を開く` から、ログ保存日数、加工抽出ロジックと素板抽出ロジックの `R2List` 抽出条件を確認・変更できます。ログ保存日数は既定で30日です。アプリ起動時に `logs` 配下の `tks_to_kintone_*.log` から、設定日数より古いログを自動削除します。抽出条件の初期値は `config.env` で指定したOLAPリクエストテンプレートの現在値です。変更できるのは抽出条件の値で、`受注No` 条件は実行時に画面入力された伝票番号へ差し替えるため編集不可です。`初期値に戻す` を押すと保存済みの変更を破棄し、テンプレートの値に戻します。

更新確認は、機能選択画面（起動直後に表示される画面）が表示されたタイミングでバックグラウンドスレッドにより1回だけ自動実行します。アプリ配布管理のkintoneアプリに登録された `TksToKintone` の最新バージョンを確認し、現在のバージョンコードより大きい配布レコードがある場合のみ更新確認ダイアログを表示します。最新の場合は何も表示しません。更新確認に失敗しても致命エラーにはせず、機能選択画面はそのまま使用できます。設定画面の `更新確認` からも手動で確認できます。更新は、ユーザーが更新確認ダイアログで「はい」を選んだ場合だけ開始します。

### PowerShell を使わない更新方式

以前は更新時に `run_update.ps1` を生成し PowerShell（`ExecutionPolicy Bypass`）で実行していましたが、DeepInstinct に PowerShell 実行がブロックされるため廃止しました。現在は PowerShell・一時スクリプト・`ExecutionPolicy Bypass` を一切使わず、通常のファイル操作とプロセス起動だけで更新します。

1. アプリモーダルの進捗画面を表示し、workerスレッドがインストーラを `.part` へダウンロードします。実受信バイト数、Content-Length、配布レコードのファイルサイズを照合します。
2. 配布レコードの `SHA-256` とPE形式を検証し、成功後だけ正式なEXE名へ置換します。Authenticode署名の有無は更新可否に影響しません。認証トークン、HTTPヘッダー、完全なダウンロードURLはログやコマンドラインへ出しません。
3. 検証workerの完了後、未保存編集の確認と実行中workerの有無を確認します。終了をキャンセルした場合はインストーラを削除し、UACを表示せず通常操作へ戻ります。
4. 終了可能と確定した後だけ、別workerがWindowsの管理者確認を表示し、インストーラを `/SILENT /SUPPRESSMSGBOXES /NORESTART /SP- /RELAUNCHAPP=1` と試行ごとに一意な `/LOG=...\update_installer_<日時>_<PID>_<識別子>.log` 付きで起動します。
5. `ShellExecuteExW` の成功、`hInstApp > 32`、プロセスハンドル取得、プロセス生存、一意なSetupログの生成（非空）を確認してから旧アプリを終了します。UAC拒否や15秒以内にログを確認できない場合はアプリを終了しません。
6. インストーラ起動成功後は終了をコミットし、再確認なしで設定保存・worker整理・単一起動ロック解放・ログflushを行います。UAC拒否や起動失敗時は終了しません。
7. セットアップとSumatraPDF依存処理が正常終了した場合だけ、Inno Setupの `runasoriginaluser` で `TksToKintone.exe --post-update` を通常ユーザー権限で起動します。通常の手動インストールでは自動起動しません。

配布管理には、`tks-to-kintone-setup.exe` とその64桁SHA-256を `SHA-256` フィールドへ登録します。コード署名は任意です。

## 出力とログ

作業ファイルは `work`、ログは `logs`、登録失敗CSVは `error` に保存します。既存の `outputTksToKintone.csv` は上書き前に日時付きで退避します。実行履歴として `work\input_yyyyMMdd_HHmmss.csv` も保存します。

登録失敗時は `error\failed_yyyyMMdd_HHmmss.csv` に失敗レコードを出力します。再実行時は `検索キー` を使って既存レコードを更新します。

## 印刷（SumatraPDFについて）

PDF印刷には、TksToKintoneとは別のWindowsアプリであるSumatraPDFを使用します。セットアップは固定版の公式64bit installerを内部に収録しているため、インストール中のインターネット接続は不要です。

- 新規インストール・更新のどちらでもSumatraPDF.exeの実在を確認し、未導入または登録が壊れている場合だけ自動導入します。既存の有効なSumatraPDFは更新・再設定しません。
- 印刷時は明示設定、HKCU、HKLM、`LOCALAPPDATA`、`Program Files`の順でSumatraPDF.exeを検出します。見つからない場合は印刷を失敗として確定し、セットアップの再実行を案内します。
- TksToKintoneをアンインストールしてもSumatraPDFは残ります。
- 第三者ソフトウェア通知・入手先は `third_party_licenses/SumatraPDF.txt` を参照してください。
- ビルド時は `scripts/sumatra_config.py` の固定URL・SHA-256を使って公式installerを検証し、`build/vendor/sumatra/`へ配置します。検証失敗時はセットアップを生成しません。

## 開発実行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
run_dev.bat
```

CSV変換だけを確認する場合:

```bash
python -m tks_to_kintone transform --glass docs/samples/素板抽出ロジックCSVサンプル.csv --processing docs/samples/加工抽出ロジックCSVサンプル.csv --output work/outputTksToKintone.csv
```

## 配布時のセキュリティ製品対応

DeepInstinctなどのEDR/AVでブロックされる場合は、未署名のPyInstaller生成exeや自己更新処理が誤検知されている可能性があります。正規配布として以下を行ってください。

1. 会社名義のコード署名証明書で `TksToKintone.exe` とインストーラを署名します。
2. 配布管理kintoneには、個別exeではなく署名済みインストーラを添付します。
3. DeepInstinctの管理画面で、署名者またはハッシュを許可リストに登録します。
4. 検知名、ブロックログ、署名済みファイルをDeepInstinctへ誤検知として提出します。
5. 利用者への配布は、DeepInstinctで許可済みの社内配布経路から行います。

`build_exe.bat` のコード署名は任意です。署名設定がなければ、signtoolや証明書がない環境でも未署名セットアップを正常に生成します。署名する場合だけ証明書ストアのSubject／拇印またはPFXを指定できます。

```bat
rem 既定: 未署名でビルド
build_exe.bat

rem 任意: 証明書ストアの拇印を優先指定
set SIGN_CERT_THUMBPRINT=証明書のSHA-1拇印
build_exe.bat

rem 任意: PFXを明示指定
set SIGN_CERT_PATH=C:\path\to\code-signing-cert.pfx
set SIGN_CERT_PASSWORD=証明書パスワード
set SIGNTOOL_PATH=C:\Program Files (x86)\Windows Kits\10\bin\x64\signtool.exe
build_exe.bat
```

正式配布用インストーラは `normal` で作成します。`all` は正式配布用と no-update 検証用を順番に作成します。

```bat
build_exe.bat normal
build_exe.bat no-update
build_exe.bat with-helper
build_exe.bat all
```

出力ファイル名は `installer\tks-to-kintone-setup.exe`、`installer\tks-to-kintone-setup-no-update.exe`、`installer\tks-to-kintone-setup-with-helper.exe` です。`normal` は helper なし自動更新版、`no-update` は更新確認と更新モジュール同梱を無効化した版、`with-helper` は過去方式の検証用です。

Inno Setupで作成したインストーラも同じ証明書で署名してください。

## mockモード確認手順

`mock` モードではTKSへ接続せず、指定したローカルCSVでCSV加工とDRY_RUNを確認します。

1. `config.env` を開き、`TKS_CLIENT_MODE=mock` を設定します。
2. `TKS_KAKOU_CSV_URL` に加工抽出ロジックCSVの `file://` フルパスを設定します。
3. `TKS_SOBA_CSV_URL` に素板抽出ロジックCSVの `file://` フルパスを設定します。
4. `KINTONE_DOMAIN`、`KINTONE_APP_ID`、`KINTONE_API_TOKEN` はDRY_RUNでも設定必須です。検証だけならダミー値でも起動確認はできます。
5. GUIを起動し、`TKS_CLIENT_MODE` が `mock` と表示されていることを確認します。
6. OLAPログインID/パスワード、伝票番号、仕上日、出荷区分を入力します。mockではログインID/パスワードはTKS送信されません。
7. `DRY_RUN` をONにしたまま `実行` を押します。
8. 登録前確認画面で伝票番号ごとの仕上日/出荷区分を確認します。kintoneへ送らない場合は `登録キャンセル` を押します。
9. `work\outputTksToKintone.csv`、画面ログ、`logs` 配下のログを確認します。

## httpモード確認手順

`http` モードではTKS OLAPへ実接続します。実通信は [app/tks_client.py](app/tks_client.py) の `HttpTksClient` に分離しています。ログインは `POST {TKS_BASE_URL}/c/ログイン認証`、OLAP取得は `PUT {TKS_BASE_URL}/c/OLAPデータ` です。

1. `config.env` を開き、`TKS_CLIENT_MODE=http` を設定します。
2. `TKS_BASE_URL` を設定します。例: `https://www.ap.tkscloud8.aga-sys.com`
3. `TKS_LOGIN_AUTH_KBN`、`TKS_TERMINAL_ID`、必要に応じて `TKS_COMPUTER_NAME`、`TKS_IP_ADDRESS` を設定します。
4. 加工用に `TKS_KAKOU_REQUEST_TEMPLATE=docs/olap/kakou_request_template.json` を設定します。
5. 素板用に `TKS_SOBA_REQUEST_TEMPLATE=docs/olap/soba_request_template.json` を設定します。
6. GUIを起動し、`TKS_CLIENT_MODE` が `http`、ProgramDataフォルダが想定パスになっていることを確認します。
7. まず `TKS接続テスト` を実行し、成功後に `OLAP取得テスト` を実行します。
8. `OLAP取得テスト` が成功したら、`DRY_RUN=true` のまま `実行` し、登録前確認画面で `outputTksToKintone.csv` の登録内容を確認します。
9. 確認後にkintoneへ送信する場合は登録前確認画面の `登録` を押します。確認なしで登録する場合のみ `DRY_RUN` をOFFにします。

## TKS接続テスト手順

1. `TKS_CLIENT_MODE=http` に設定します。
2. GUIを起動し、OLAPログインIDとOLAPパスワードを入力します。
3. `TKS接続テスト` を押します。
4. 画面ログで以下を確認します。
   - ログインURL
   - HTTPメソッド `POST`
   - HTTPステータスコード
   - Content-Type
   - レスポンス先頭500文字
   - JSONトップレベルキー
   - `.ASPXAUTH` Cookie取得有無
5. Cookie値、パスワード、トークン値はログに出ません。
6. 失敗した場合は `work\debug\login_response_*.json` と `logs` 配下のログを確認します。

## OLAP取得テスト手順

1. `TKS接続テスト` が成功する状態にします。
2. 伝票番号を1件以上入力します。複数行またはカンマ区切りに対応しています。
3. `OLAP取得テスト` を押します。
4. 加工/素板のOLAP取得だけを実行します。kintone登録と `outputTksToKintone.csv` 作成は行いません。
5. 画面ログで以下を確認します。
   - OLAPデータ取得URL
   - HTTPメソッド `PUT`
   - 対象伝票番号件数
   - HTTPステータスコード
   - Content-Type
   - レスポンス先頭500文字
   - JSONトップレベルキー
   - CSV保存先
   - CSV行数
6. レスポンス本文は `work\debug\kakou_response_*.txt`、`work\debug\soba_response_*.txt` に保存されます。
7. CSV化できた場合は `work\kakou_extract.csv` と `work\soba_extract.csv` が作成されます。

## 要確認事項

既存VBAは列位置ベースの処理を含むため、CSV入力列はヘッダー名でチェックしています。サンプルCSVでは重複する `掛率集計コード`、`掛率集計名称` を読み込み時に `_1` 付き列として扱い、出力では `掛率集計コード_1`、`掛率集計名称_2` に合わせています。TKS本番CSVのヘッダー名が異なる場合は `tks_to_kintone/transform.py` の列定数を確認してください。

## 次に必要な作業

TKS実接続の前に、以下を実環境または通信ログで確認してください。

- ベースURL: `docs/olap/olap_extract.ps1` では `BaseUrl` に `/c/ログイン認証` と `/c/OLAPデータ` を連結しています。GUI版も `TKS_BASE_URL` にベースURLを設定します。
- ログインPOST先: 既存PowerShellでは `POST {BaseUrl}/c/ログイン認証`、`Content-Type: application/json; charset=utf-8`、`Accept: application/json` です。
- ログインJSONキー: `契約会社コード`、`ログインID`、`パスワード`、`ログイン認証区分`、`端末識別ID`、`コンピュータ名`、`IPアドレス`、`ScreenName` です。
- ログイン成功判定: JSONの `ResponseData` が存在し、`ResponseData.Ｘ0` が `00`、かつCookie `.ASPXAUTH` が取得できることです。
- セッション維持: PowerShellの `WebRequestSession` 相当として、Python側は `requests.Session()` でCookieを保持します。
- OLAPデータ取得: `PUT {BaseUrl}/c/OLAPデータ` に、`docs/olap` 配下のJSONテンプレートを送信します。
- CSV取得仕様: TKSはJSONレスポンスの `ResponseData.R1List` に表形式データを返します。アプリ側でCSVへ変換します。
- 加工/素板の抽出条件: `R1List` と `R2List` はテンプレートJSONに固定しています。実行時に変更するのは `R2List` 内の `フィールド論理名=受注No` の `OLAP値` だけです。加工抽出ロジックでは `OP区分` 条件を除外しています。
- CSV文字コード: 本番レスポンスが `cp932` かUTF-8かを確認してください。現在の既定値は `CSV_ENCODING=cp932` です。
- 実装差し替え箇所: モックは `MockTksClient`、実通信は `HttpTksClient` に分離済みです。実TKS仕様で追加条件や列定義が必要な場合は `docs/olap/kakou_request_template.json` または `docs/olap/soba_request_template.json` を更新してください。

## TKS実接続のデバッグ

`TKS_CLIENT_MODE=http` の場合、失敗時調査用に以下を `work\debug` に保存します。

- `login_response_yyyyMMdd_HHmmss.json`
- `kakou_response_yyyyMMdd_HHmmss.txt`
- `soba_response_yyyyMMdd_HHmmss.txt`

保存前にパスワード、Cookie、トークンらしき値はマスクします。HTTP接続で必須設定が不足している場合は、起動時に不足キー名を表示して停止します。
