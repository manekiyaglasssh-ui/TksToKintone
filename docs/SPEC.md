# 現行バージョン追補（1.6.2 / ビルド46）

現行バージョンの基準は `app/version.py` とする。SumatraPDFはTksToKintone配下へ同梱せず、固定・検証済みの公式installerをセットアップへ収録する。新規・更新を問わず、有効なSumatraPDF.exeが見つからない場合だけセットアップから全ユーザー用に導入し、終了コードと導入後のexe実在を確認する。印刷時は明示設定、HKCU、HKLM、LOCALAPPDATA、Program Filesの順で独立インストール版を探索し、旧ポータブル版は使用しない。

TksToKintoneのアンインストールではSumatraPDFを削除しない。更新時は依存導入の成功確認後、TksToKintoneが旧版で管理していた `{app}\tools\SumatraPDF` と `{app}\_internal\tools\SumatraPDF` だけを削除する。

## 1.5.12で追加した仕様

現行バージョンの基準は `app/version.py` とする。伝票へ表示する加工名は1～12行目ごとに設定できる。表示名の変更は内部加工キー・加工判定・Kintone登録値へ影響させない。

指図書編集は、ドラッグ矩形に合わせた文字サイズ調整、右クリック編集、リサイズハンドルによる文字サイズ変更、拡張ヒット領域、お気に入り登録位置の保存、未保存snapshotの直接プレビューに対応する。左ペインの反映先とお気に入りはセクション内だけで並び替え可能とし、stable key／stable IDの順序をQSettingsへ保存する。並び替えは編集データ、PDF、Undo／Redoへ含めない。

## 1.5.11で追加した仕様

指図書編集画面は背景生成とフォント一覧取得を遅延・キャッシュし、伝票一覧は最初の10件を表示後、残りを10件単位で読み込む。Kintone登録前確認画面は先に表示し、OLAP・Kintone確認と後続行準備をバックグラウンドで行う。Kintone登録実行は全件の確認・検証完了まで無効とする。

指図書のPDF・プレビュー・印刷では、日本語フォント、TTC内のスタイル別フェイス、文字ごとのglyph fallbackを適用する。出力時は保存済みの最新編集内容を再読込し、古い編集内容やプレビューキャッシュを使用しない。

## 1.5.9で追加した仕様

現行バージョンの基準は `app/version.py` とする。売上伝票（01）、工場控（02）、納品書（07）の単価列・金額列上段に表示する㎡は、文字列をNFKC正規化して前後空白を除去したOP区分が `00`、`01`、`02` の明細だけを対象とする。この判定は上段㎡の描画だけに作用し、単価・明細金額・合計金額、内部計算、CSV、Kintone登録データを変更しない。

指図書編集のテキストは `font_family`、`font_size`、`bold`（太字）を保持し、画面表示、保存・再読込、Undo／Redo、独立コピー、PDF出力へ適用する。フォント一覧にはOSのインストール済みフォントを使用し、未インストールフォントは既定フォントへフォールバックする。文字サイズは4～200ptとする。旧保存データで追加属性がない場合は従来の既定値を使用する。

フォントのお気に入りは `voucher_edit/favorite_fonts` のQStringListとしてQSettingsへ登録順で保存する。フォント欄左側の☆／★で追加・解除する。フォント選択は1つのQComboBoxへ統合し、選択不可の見出しとセパレーターを使って、お気に入りを登録順で上部、QFontDatabaseの全フォントをその下へ表示する。各フォント項目は自身のフォントで表示する。OSに存在しない登録値は読み込み時に除外して設定を整理する。お気に入り設定は伝票編集データ、Undo／Redo、コピー、PDF生成データへ含めない。

☆／★はフォント欄の左へ配置し、透明背景・枠線なしのコンパクトなQToolButtonとする。テキストの `font_bold`、`font_italic`、`font_underline`、`font_strikeout` は画面、保存、独立コピー、Undo／Redo、PDFへ反映する。太字・斜体・下線・取り消し線は1つのQToolButton＋QMenuへcheckable QActionとして集約し、同じQActionを右クリックとCtrl+B／Ctrl+I／Ctrl+U／Ctrl+5で共有する。PDFではBold／Italicに対応するフォントフェイスを解決し、下線と取り消し線は文字幅・基準線・文字色に合わせたReportLabの線として描画する。追加属性のないschema version 3以前のオブジェクトは各装飾OFFとして読み込む。

---

以下の仕様で、Windows向けの「TKS OLAP → CSV加工 → kintone登録」ツールを作成してください。

目的：
現在Excel VBAで行っている処理を廃止し、Python製のWindowsアプリとして実装する。
使用者PCにはPythonが入っていないため、最終的にはPyInstallerでexe化し、Inno Setupでインストーラ配布する。

重要：
- 既存VBAの処理を勝手に簡略化しないこと
- 可能な限りCSVヘッダー名ベースで処理すること
- 列順依存が必要な場合は、列名定数と列順チェックを入れること
- 必須列チェックを実装し、不足列がある場合は分かりやすいエラーを出すこと
- 認証情報やAPIトークンはソースコードに直書きしないこと
- パスワードやAPIトークンをログに出さないこと
- exe実行時のカレントディレクトリに依存しないこと
- 処理本体とGUIを分離し、将来的にCLI版も作れる構成にすること

前提：
- Windows環境で動作
- 使用者PCにはPython未インストール
- GUI付きアプリとして起動する
- GUIライブラリは PySide6 を使用する
- 設定ファイルは C:\ProgramData\Manekiya\TksToKintone 配下に配置する
- ログ、作業ファイル、エラーCSVも ProgramData 配下に保存する
- 契約会社コードは固定値として config.env に保存する
- OLAPログインID、OLAPパスワード、伝票番号、仕上日、出荷区分は画面で入力する
- kintone APIトークンやURLなどの固定設定は config.env に保存する

作成する構成：

tks-to-kintone/
├─ app/
│  ├─ main.py
│  ├─ gui.py
│  ├─ config.py
│  ├─ tks_client.py
│  ├─ csv_processor.py
│  ├─ kintone_client.py
│  ├─ logger.py
│  └─ models.py
├─ templates/
│  ├─ config.env.sample
│  └─ field_mapping.json.sample
├─ installer/
│  └─ tks-to-kintone.iss
├─ build_exe.bat
├─ run_dev.bat
├─ requirements.txt
└─ README.md

画面項目：

1. 契約会社コード
   - config.env から読み込む
   - 画面に表示する
   - 基本的に編集不可にする

2. OLAPログインID
   - 画面で入力する
   - 必須

3. OLAPパスワード
   - 画面で入力する
   - 必須
   - 入力欄は伏せ字表示にする
   - ログには絶対に出さない

4. 伝票番号
   - 複数入力可能にする
   - 複数行テキスト欄にする
   - 1行に1伝票番号で入力できるようにする
   - カンマ区切りにも対応する

5. 仕上日
   - 画面で入力する
   - 日付入力欄にする
   - 形式は yyyy-MM-dd

6. 出荷区分
   - 画面で入力する
   - コンボボックスにする
   - 候補値は config.env で変更できるようにする
   - 初期候補は以下
     - AM
     - PM

7. DRY_RUN
   - チェックボックスにする
   - 初期値は ON
   - ONの場合、kintoneへ実登録せず、outputTksToKintone.csv作成まで行う
   - OFFの場合、kintoneへ登録する

8. 実行ボタン
   - 押下すると処理開始
   - 処理中は二重実行できないようにボタンを無効化する

9. ログ表示欄
   - 画面下部に実行ログを表示する
   - logsフォルダにも同じ内容を保存する

10. 結果表示
   - 完了時に以下を表示する
     - outputTksToKintone.csv の出力件数
     - kintone登録成功件数
     - kintone登録失敗件数
     - エラー有無
     - ログファイルパス

処理フロー：

1. C:\ProgramData\Manekiya\TksToKintone\config.env を読み込む
2. 画面で以下を入力する
   - OLAPログインID
   - OLAPパスワード
   - 伝票番号
   - 仕上日
   - 出荷区分
3. TKS OLAPにログインする
4. 入力された伝票番号を複数指定して、加工抽出ロジックCSVを取得する
5. 入力された伝票番号を複数指定して、素板抽出ロジックCSVを取得する
6. 取得した「加工抽出ロジックCSV」「素板抽出ロジックCSV」を使って、既存VBAと同じ加工処理を行う
7. outputTksToKintone.csv を出力する
8. outputTksToKintone.csv の各行に、画面入力された「仕上日」「出荷区分」を追加する
9. DRY_RUNがOFFの場合、kintone REST APIへAPIトークン認証でレコード登録する
10. 成功件数、失敗件数、エラー内容を画面とログに出力する

config.env の形式：

TKS_COMPANY_CODE=G29V-T8GL-9LYD

TKS_LOGIN_URL=
TKS_KAKOU_CSV_URL=
TKS_SOBA_CSV_URL=

KINTONE_DOMAIN=
KINTONE_APP_ID=
KINTONE_API_TOKEN=

OUTPUT_DIR=work
LOG_DIR=logs
ERROR_DIR=error

CSV_ENCODING=cp932

SHUKKA_KBN_OPTIONS=通常,直送,引取,その他
DRY_RUN_DEFAULT=true

補足：
- TKS_COMPANY_CODE は契約会社コード
- 契約会社コードは固定値として扱う
- OLAPログインIDとOLAPパスワードは config.env には保存しない
- OLAPログインIDとOLAPパスワードは毎回画面入力する
- 将来的にログインIDだけ保存、またはWindows資格情報マネージャーに保存できるよう、設計上は分離しておく

画面入力値から内部的に作る入力データ：

denpyo_no,shiage_date,shukka_kbn
1386680,2026-05-20,通常
1386681,2026-05-20,通常

input.csv は必須ファイルにしない。
ただし、実行履歴確認用として work/input_yyyyMMdd_HHmmss.csv に保存する。

実装要件：

- Python 3.11 以上を想定
- GUIは PySide6 を使用
- HTTP通信は requests を使用
- .env 読み込みは python-dotenv を使用
- CSV処理は pandas ではなく、標準 csv モジュールを優先
- Shift-JIS / CP932 のCSVを正しく扱う
- ログは画面、標準出力、ファイルのすべてに出す
- 例外発生時もログに詳細を残す
- kintone登録は100件単位で分割し、検索キーを更新キーにしてPUT upsertする
- 登録失敗したレコードは error フォルダに failed_yyyyMMdd_HHmmss.csv として保存する
- outputTksToKintone.csv は work フォルダに保存する
- 既存ファイルは上書き前に日時付きで退避する
- config.env が存在しない場合は config.env.sample をコピーして作成し、設定不足として終了する
- field_mapping.json が存在しない場合は field_mapping.json.sample をコピーして作成し、設定不足として終了する
- logs、work、error フォルダは自動作成する

main.py / gui.py の方針：

- main.py はGUIアプリの起動のみを行う
- 画面定義とイベント処理は gui.py に分離する
- 処理本体はGUIに直接書かない
- 処理中に画面が固まらないよう、QThread または worker thread を使う
- 処理中は実行ボタンを無効化する
- 処理完了後は実行ボタンを再度有効化する

処理本体の分離：

- tks_client.py
  - TKSログイン
  - 加工CSV取得
  - 素板CSV取得

- csv_processor.py
  - CSV加工
  - outputTksToKintone.csv作成

- kintone_client.py
  - kintone登録

- config.py
  - ProgramDataパス解決
  - config.env 読み込み
  - 必須設定チェック

- logger.py
  - ファイルログ
  - 画面ログ連携

- models.py
  - 入力データ、処理結果などのデータクラス

TKS OLAPログイン・CSV取得：

- requests.Session() を使い、ログイン後のCookieを保持する
- ログイン時は以下を使用する
  - 契約会社コード: TKS_COMPANY_CODE
  - ログインID: 画面入力値
  - パスワード: 画面入力値
- ログイン成功・失敗を判定する
- 伝票番号は複数指定できるようにする
- CSV取得URLやPOSTパラメータは tks_client.py に集約する
- URLや固定値は config.env または tks_client.py 冒頭で変更しやすくする
- TKS OLAPの実際のログインURL、POSTパラメータ、CSV取得URLが未確定の場合は、tks_client.py に TODO として明確に分離し、後から差し替えやすい構造にする
- 既存の olap_extract.ps1 がある場合は、そのログイン処理・Cookie処理・CSV取得処理をPythonに移植できるようにする

CSV加工処理：

csv_processor.py に、既存VBA相当の処理を実装する。

対象：
- 加工抽出ロジックCSV
- 素板抽出ロジックCSV

処理内容：
- 加工抽出ロジックCSV、素板抽出ロジックCSVを読み込む
- VBAの LIST シート相当のデータ構造を作る
- 発注数を受注数から設定する
- 硝子本体行の商品名称を加工行の商品名称へ補正する
- 品種区分が 5 の場合、以下を差し替える
  - 掛率集計コード = 掛率集計コード_1
  - 掛率集計名称 = 掛率集計名称_2
  - 発注先コード = 加工完成品仕入先コード
- 硝子/加工区分、次行の硝子/加工区分、発注先コード、本社発注コード判定を使って判定列を作る
- 加工行には親の硝子本体行の判定を引き継ぐ
- 判定が「-」の行は outputTksToKintone.csv に出力しない
- 画面入力された仕上日、出荷区分を outputTksToKintone.csv に追加する
- 最終的に outputTksToKintone.csv を出力する

注意：
既存VBAの列名、判定条件、出力列順は、mdlCommon.bas と shtMENU.cls を確認して完全に合わせること。
不明な列名がある場合は、列名を定数化し、READMEに要確認事項として残すこと。

kintone登録：

kintone_client.py に実装する。

- APIトークン認証を使う
- エンドポイントは以下を使う
  - https://{KINTONE_DOMAIN}/k/v1/records.json
- 100件単位で登録する
- 検索キーをupdateKeyに指定し、既存があれば更新、なければ追加する
- CSV列名とkintoneフィールドコードの対応表を field_mapping.json にする
- field_mapping.json.sample を作成する
- 登録成功件数、失敗件数をログに出す
- APIエラー時はレスポンス本文をログに出す
- APIトークン自体はログに出さない
- DRY_RUNがtrueの場合は、kintoneへ送信せず、登録予定件数だけログに出す

field_mapping.json.sample の例：

{
  "伝票番号": "denpyo_no",
  "商品名称": "product_name",
  "発注数": "order_qty",
  "仕上日": "shiage_date",
  "出荷区分": "shukka_kbn"
}

bat / exe / installer：

- run_dev.bat を作成する
  - 開発時に python app/main.py を実行する
- build_exe.bat を作成する
  - venv作成
  - pip install -r requirements.txt
  - pyinstallerでexe作成
- PyInstallerは onefile ではなく onedir を推奨する
- exe名は TksToKintone.exe にする
- Inno Setup用の installer/tks-to-kintone.iss を作成する
- インストール先は以下にする
  - C:\Program Files\Manekiya\TksToKintone
- ユーザーが編集する設定ファイルは以下に配置する
  - C:\ProgramData\Manekiya\TksToKintone
- 初回インストール時に以下を ProgramData に配置する
  - config.env.sample
  - field_mapping.json.sample
- config.env、field_mapping.json が存在しない場合は sample からコピーして作成する
- logs、work、error も ProgramData 配下に作成する
- スタートメニューにショートカットを作成する
- デスクトップショートカットも作成する

ログ出力：

ログには以下を出す。

- 起動日時
- 対象伝票番号
- 仕上日
- 出荷区分
- TKSログイン成功/失敗
- 加工CSV取得成功/失敗
- 素板CSV取得成功/失敗
- outputTksToKintone.csv 出力件数
- DRY_RUN状態
- kintone登録成功件数
- kintone登録失敗件数
- エラー詳細
- 完了日時

ログに出してはいけないもの：

- OLAPパスワード
- KINTONE_API_TOKEN
- その他パスワード、トークン類

README.md に書く内容：

- インストール方法
- config.env の設定方法
- field_mapping.json の書き方
- 画面の入力方法
- 伝票番号を複数入力する方法
- DRY_RUNの使い方
- 実行方法
- ログ確認方法
- エラー時の確認ポイント
- 再実行時の注意点
- kintone APIトークンに必要な権限
- TKS OLAP側のURLやPOSTパラメータが未確定の場合の修正箇所

まずは以下を作成してください。

1. 全体のフォルダ構成
2. Pythonコード一式
3. PySide6のGUI画面
4. sample設定ファイル
5. field_mapping.json.sample
6. build_exe.bat
7. run_dev.bat
8. Inno Setup用 .iss
9. README.md

TKS OLAPの実際のログインURL、POSTパラメータ、CSV取得URLが未確定の場合は、仮実装で構いません。
ただし、tks_client.py に TODO として明確に分離し、後から差し替えやすい構造にしてください。

実装後、以下を確認してください。

- python -m py_compile app/*.py が通ること
- サンプルCSVだけで csv_processor.py の処理テストができること
- TKS通信部分は未確定ならモックCSVで処理できること
- DRY_RUN=true の場合、kintoneへ実登録せずログだけ出ること
