# TKS OLAP to kintone インストーラ作成手順

## 1. 概要

本手順は、TKS OLAP to kintone の実行ファイルおよびインストーラを作成するための手順です。

作成対象：

- 実行ファイル  
  `dist\TksToKintone\TksToKintone.exe`

- インストーラ  
  `installer\tks-to-kintone-setup_[バージョンネーム].exe`

例：

```text
tks-to-kintone-setup_1.0.0.exe
```

---

## 2. 前提条件

開発PCに以下がインストールされていること。

- Python
- 必要なPythonライブラリ
- PyInstaller
- Inno Setup

プロジェクトフォルダ例：

```text
C:\Users\U021\work\TksToKintone
```

---

## 3. バージョン情報の変更

### 3.1 app/version.py を変更する

以下のファイルを開く。

```text
app\version.py
```

バージョンネームとバージョンコードを変更する。

例：

```python
VERSION_NAME = "1.0.0"
VERSION_CODE = 1
```

変更例：

```python
VERSION_NAME = "1.0.1"
VERSION_CODE = 2
```

### 注意

- `VERSION_NAME` は画面表示やインストーラ名に使用する。
- `VERSION_CODE` は内部管理用の連番として使用する。
- バージョンアップ時は、原則として `VERSION_CODE` も増やす。

---

## 4. exe作成

コマンドプロンプトを開き、プロジェクトフォルダへ移動する。

```bat
cd /d C:\Users\U021\work\TksToKintone
```

必要に応じて、古いビルド結果を削除する。

```bat
rmdir /s /q build
rmdir /s /q dist
```

以下を実行する。

```bat
build_exe.bat
```

成功すると、以下に exe が作成される。

```text
dist\TksToKintone\TksToKintone.exe
```

---

## 5. exeの動作確認

作成された exe を起動する。

```bat
dist\TksToKintone\TksToKintone.exe
```

以下を確認する。

- アプリが起動する
- 画面タイトルが正しい
- バージョン表示が正しい
- 設定画面が開ける
- DRY_RUN で実行できる
- エラーなくCSV出力まで完了する

---

## 6. Inno Setup定義ファイルの変更

以下のファイルを開く。

```text
installer\tks-to-kintone.iss
```

バージョンネームを変更する。

例：

```ini
#define MyAppVersion "1.0.0"
```

変更例：

```ini
#define MyAppVersion "1.0.1"
```

インストーラの出力ファイル名も、以下の命名規則に合わせる。

```ini
OutputBaseFilename=tks-to-kintone-setup_1.0.1
```

## 命名規則

```text
tks-to-kintone-setup_[バージョンネーム].exe
```

例：

```text
tks-to-kintone-setup_1.0.0.exe
tks-to-kintone-setup_1.0.1.exe
tks-to-kintone-setup_1.1.0.exe
```

---

## 7. インストーラ作成

Inno Setup Compiler を起動する。

以下のファイルを開く。

```text
installer\tks-to-kintone.iss
```

メニューから以下を実行する。

```text
Build → Compile
```

または、キーボードで `F9` を押す。

成功すると、以下にインストーラが作成される。

```text
installer\tks-to-kintone-setup_[バージョンネーム].exe
```

例：

```text
installer\tks-to-kintone-setup_1.0.1.exe
```

---

## 8. コマンドでインストーラを作成する場合

Inno Setupをコマンドで実行する場合は、以下を実行する。

```bat
cd /d C:\Users\U021\work\TksToKintone\installer

"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tks-to-kintone.iss
```

---

## 9. インストーラの動作確認

作成されたインストーラを実行する。

```text
installer\tks-to-kintone-setup_[バージョンネーム].exe
```

以下を確認する。

- インストールが完了する
- スタートメニューまたはショートカットから起動できる
- アプリが正常に起動する
- 設定画面が開ける
- ProgramData配下のフォルダが作成される
- DRY_RUNで実行できる
- 必要に応じてkintoneテスト環境へ登録・更新できる

---

## 10. インストール先

アプリ本体のインストール先：

```text
C:\Program Files\Manekiya\TksToKintone
```

設定・ログ・作業フォルダ：

```text
C:\ProgramData\Manekiya\TksToKintone
```

主なフォルダ：

```text
C:\ProgramData\Manekiya\TksToKintone\config.env
C:\ProgramData\Manekiya\TksToKintone\logs
C:\ProgramData\Manekiya\TksToKintone\work
C:\ProgramData\Manekiya\TksToKintone\error
```

---

## 11. 配布前チェックリスト

配布前に以下を確認する。

- [ ] `app/version.py` の `VERSION_NAME` を変更した
- [ ] `app/version.py` の `VERSION_CODE` を増やした
- [ ] `build_exe.bat` を実行した
- [ ] `dist\TksToKintone\TksToKintone.exe` が作成された
- [ ] exe単体で起動確認した
- [ ] `installer\tks-to-kintone.iss` のバージョンを変更した
- [ ] `OutputBaseFilename` を命名規則どおりに変更した
- [ ] Inno SetupでCompileした
- [ ] `installer\tks-to-kintone-setup_[バージョンネーム].exe` が作成された
- [ ] インストーラでインストール確認した
- [ ] インストール後にアプリが起動することを確認した
- [ ] DRY_RUNで正常実行できることを確認した
- [ ] 必要に応じてテストkintoneで登録・更新確認した

---

## 12. 注意事項

### APIトークン

kintone APIトークンはインストーラやソースに直接含めない。

本番運用時は、以下の設定ファイルで管理する。

```text
C:\ProgramData\Manekiya\TksToKintone\config.env
```

### ログ出力禁止情報

以下はログやdebugファイルに出力しない。

- OLAPパスワード
- kintone APIトークン
- Cookie値
- .ASPXAUTHの値

### 本番登録前の確認

本番kintoneへ登録・更新する場合は、以下を確認する。

- Kintone接続先が本番になっているか
- DRY_RUNがOFFになっているか
- 伝票番号が正しいか
- 仕上日が正しいか
- 出荷区分が正しいか
- 登録前確認画面の内容が正しいか

---

## 13. 標準作業コマンドまとめ

```bat
cd /d C:\Users\U021\work\TksToKintone

rmdir /s /q build
rmdir /s /q dist

build_exe.bat
```

exe確認：

```bat
dist\TksToKintone\TksToKintone.exe
```

インストーラ作成：

```bat
cd /d C:\Users\U021\work\TksToKintone\installer

"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" tks-to-kintone.iss
```

作成されるインストーラ：

```text
installer\tks-to-kintone-setup_[バージョンネーム].exe
```
