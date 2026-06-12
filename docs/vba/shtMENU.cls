VERSION 1.0 CLASS
BEGIN
  MultiUse = -1  'True
END
Attribute VB_Name = "shtMENU"
Attribute VB_GlobalNameSpace = False
Attribute VB_Creatable = False
Attribute VB_PredeclaredId = True
Attribute VB_Exposed = True
Option Explicit

Public Sub cmdExport()

    ' --- 変数の宣言 ---
    Dim exportPath As String
    Dim folderPath As String ' フォルダパスを格納する変数を追加
    Dim defaultPath As String
    Dim fileNum As Integer
    Dim lastRow As Long
    Dim lastCol As Long
    Dim r As Long
    Dim c As Integer
    Dim outputText As String
    
    Const C_RANGE_KEY_FILEPATH = "F11"
    
    ' --- 事前設定 ---
    ' A10セルから前回のパスを読み込む
    defaultPath = ThisWorkbook.ActiveSheet.Range(C_RANGE_KEY_FILEPATH).Value
    
    ' 前回のパスが存在しない場合は、ドキュメントフォルダを既定にする
    If defaultPath = "" Then
        defaultPath = CreateObject("WScript.Shell").SpecialFolders("MyDocuments") & "\"
    End If
        
    ' --- 1. ファイル保存ダイアログの表示 ---
    With Application.FileDialog(msoFileDialogSaveAs)
        .Title = "カンマ区切りテキストファイルとして保存"
        .InitialFileName = defaultPath & "TksToKintone.csv" ' 既定のファイル名
        .FilterIndex = 5 ' ファイルの種類をテキストファイルに
        ' ダイアログを表示し、キャンセルされたら処理を終了
        If .Show = False Then
            Exit Sub
        End If
        
        ' 選択されたフルパスを取得
        exportPath = .SelectedItems(1)
    End With
    
     ' --- 2. 出力先の「フォルダパス」をC_RANGE_KEY_FILEPATHに保存 (★★★ 修正点 ★★★) ---
    ' FileSystemObjectを使ってファイル名を除いた親フォルダのパスを取得
    folderPath = CreateObject("Scripting.FileSystemObject").GetParentFolderName(exportPath)
    
    ' 次回のためにフォルダパスをC_RANGE_KEY_FILEPATHに保存（末尾に円マークを追加）
    ThisWorkbook.ActiveSheet.Range(C_RANGE_KEY_FILEPATH).Value = folderPath & "\"
    
    ' --- 3. シートの内容をタブ区切りでファイル出力 ---
    ' ファイル番号を取得
    fileNum = FreeFile
    
    ' ファイルを書き込みモードで開く
    Open exportPath For Output As #fileNum
    
    ' 最終行と最終列を取得
    lastRow = shtLIST.Cells.Find("*", SearchOrder:=xlByRows, SearchDirection:=xlPrevious).Row
    lastCol = shtLIST.Cells.Find("*", SearchOrder:=xlByColumns, SearchDirection:=xlPrevious).Column
    
    ' 1行ずつループ
    For r = 1 To lastRow
        outputText = ""
        '出力判定("-"は不要データ)
        If shtLIST.Range(GC_DATA_COL_HANTEI & r).Text <> "-" Then
            ' 1列ずつループ
            For c = 1 To lastCol
                ' セルの内容を追加
                outputText = outputText & """" & shtLIST.Cells(r, c).Text & """"
                
                ' 最終列でなければカンマを追加
                If c < lastCol Then
                    outputText = outputText & ","
                End If
            Next c
            
            ' 1行分のデータをファイルに書き込む
            Print #fileNum, outputText
        End If
    Next r
    
    ' ファイルを閉じる
    Close #fileNum
    
    ' --- 完了メッセージ ---
    MsgBox "ファイルの出力が完了しました。" & vbCrLf & _
            "(判定'-'は出力されません)" & vbCrLf & "保存先: " & exportPath

End Sub
'**************************************************
'   データ取り込みボタン
'**************************************************
Public Sub cmdRun()
    
    Dim lReturn As Long
    
    lReturn = MsgBox("データを取り込みますか", vbQuestion + vbYesNo + vbDefaultButton1, "TKSデータ取り込み")
    If lReturn = vbNo Then
        Exit Sub
    End If
    
On Error GoTo Err_State:

    'PowerQueryのクエリを更新(特定フォルダからのデータ)
    Application.StatusBar = "Power Queryを更新中です..."
    ThisWorkbook.Connections("クエリ - JISEKI").OLEDBConnection.BackgroundQuery = False
    ThisWorkbook.Connections("クエリ - JISEKI").Refresh
    Application.StatusBar = "Power Queryを更新完了"
    
    'シート「DATA」→シート「LIST」にデータコピー
    Application.StatusBar = "DATA→LISTにコピー中..."
    Call CopySheet(shtDATA_WK1.Name, shtLIST.Name)
    
    '後処理
    Application.StatusBar = False
    Call MsgBox("処理完了", vbInformation + vbOKOnly, "TKSデータ取り込み")

Exit Sub

Err_State:

    Call MsgBox(Err.Description, vbOKOnly, "データ取り込みボタン")
    
End Sub


'**************************************************
'   発注数→受注数設定ボタン
'**************************************************
Public Sub cmdRunHacyusu()
    
    Call SetHacyusuAndHinmei    '発注数→受注数、商品名称の整理
    Call CheckHinsyuKbn         '品種区分'5'対応
    Call CheckHantei            '結果判定(硝子本体)
    Call CheckHanteiKakou       '結果判定(加工部分)
    
    Call MsgBox("処理完了", vbInformation + vbOKOnly, "発注数→受注数設定")
    
End Sub

'**************************************************
'   判定処理(データの要不要分類)
'**************************************************
Private Function CheckHantei() As Boolean

    Dim lRowMax As Long
    Dim lRowCnt As Long
    Dim dJyucyuSu As Double
    
    lRowMax = shtLIST.Cells(Rows.Count, 1).End(xlUp).Row
    
    With shtLIST
        For lRowCnt = 2 To lRowMax
            '加工判定　IF([@[硝/加工]]="1",IF(C2="2","〇","×"),IF([@[硝/加工]]="2","〇","×"))
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS Then
                If .Range(GC_DATA_COL_GLASSKBN & lRowCnt + 1).Value = GC_GLASSKBN_KAKOU Then
                    .Range(GC_DATA_COL_KAKOUHANTEI & lRowCnt).Value = "〇"
                Else
                    .Range(GC_DATA_COL_KAKOUHANTEI & lRowCnt).Value = "×"
                End If
            ElseIf .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_KAKOU Then
                .Range(GC_DATA_COL_KAKOUHANTEI & lRowCnt).Value = "〇"
            Else
                .Range(GC_DATA_COL_KAKOUHANTEI & lRowCnt).Value = "×"
            End If
            '洗浄判定　=IF(AND([@[硝/加工]]="1",C3="2"),"1:洗浄","0:不要")
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS And _
                .Range(GC_DATA_COL_GLASSKBN & lRowCnt + 1).Value = GC_GLASSKBN_KAKOU Then
                .Range(GC_DATA_COL_SENJYOKBN & lRowCnt).Value = "1:洗浄"
            Else
                .Range(GC_DATA_COL_SENJYOKBN & lRowCnt).Value = "0:不要"
            End If
            '結果判定
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS Then
                'If Trim(.Range("AH" & lRowCnt).Value) = "〇" Then
                If Trim(.Range(GC_DATA_COL_HACYUCDHANTEI & lRowCnt).Value) = "〇" Then
                    '硝子本体が本社発注の場合
                    .Range(GC_DATA_COL_HANTEI & lRowCnt).Value = "〇(本社発注コード)"
                ElseIf .Range(GC_DATA_COL_GLASSKBN & lRowCnt + 1).Value = GC_GLASSKBN_KAKOU Then
                    '加工が発生している場合
                    .Range(GC_DATA_COL_HANTEI & lRowCnt).Value = "〇(加工あり)"
                ElseIf .Range(GC_DATA_COL_GLASSKBN & lRowCnt + 1).Value = GC_GLASSKBN_GLASS And .Range(GC_DATA_COL_HACYUCD & lRowCnt + 1).Value = "11116" Then
                    '発注先コード"11116"とのセット品の場合
                    .Range(GC_DATA_COL_HANTEI & lRowCnt).Value = "〇(セット品)"
               End If
            End If
        Next lRowCnt
    End With
    
            
End Function

'**************************************************
'   加工の表示「〇」判定
'   親情報となる硝子の判定を見て加工も同じ判定に設定
'**************************************************
Private Function CheckHanteiKakou() As Boolean

    Dim lRowMax As Long
    Dim lRowCnt As Long
    Dim szHantei As String
    
    lRowMax = shtLIST.Cells(Rows.Count, 1).End(xlUp).Row
    
    With shtLIST
        For lRowCnt = 2 To lRowMax
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS Then
                szHantei = .Range(GC_DATA_COL_HANTEI & lRowCnt).Value
            End If
            .Range(GC_DATA_COL_HANTEI & lRowCnt).Value = szHantei
        Next lRowCnt
    End With
    
            
End Function

'**************************************************
'   発注数→受注数設定,　硝子の品名を加工にもセット
'**************************************************
Private Function SetHacyusuAndHinmei() As Boolean

    Dim lRowMax As Long
    Dim lRowCnt As Long
    Dim dJyucyuSu As Double
    Dim szHinmei As String
    Dim szHinmeiTmp As String
    Dim lHinemiLen As Long
    
    lRowMax = shtLIST.Cells(Rows.Count, 1).End(xlUp).Row
    
    With shtLIST
        For lRowCnt = 2 To lRowMax
             '硝子本体の受注数を各行の発注数にセット
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS Then
                dJyucyuSu = .Range(GC_DATA_COL_JYUCYU & lRowCnt).Value
               
            End If
            .Range(GC_DATA_COL_HACYUSU & lRowCnt).Value = dJyucyuSu
            '硝子本体の商品名称を各行の商品名称に追加(ex.品番 + " " + 加工名称)
            If .Range(GC_DATA_COL_GLASSKBN & lRowCnt).Value = GC_GLASSKBN_GLASS Then
                szHinmeiTmp = StrConv(.Range(GC_DATA_COL_HINMEI & lRowCnt).Value, vbWide)
                lHinemiLen = InStr(1, szHinmeiTmp, "　")
                If lHinemiLen = 0 Then
                    szHinmei = szHinmeiTmp
                Else
                    szHinmei = Mid(szHinmeiTmp, 1, lHinemiLen - 1)
                End If
            Else
                If szHinmei <> Mid(.Range(GC_DATA_COL_HINMEI & lRowCnt).Value, 1, Len(szHinmei)) Then
                    .Range(GC_DATA_COL_HINMEI & lRowCnt).Value = szHinmei & .Range(GC_DATA_COL_HINMEI & lRowCnt).Value
                End If
            End If
        Next lRowCnt
    End With
    
End Function

'**************************************************
'   品種区分"5"対応
'**************************************************
Private Function CheckHinsyuKbn() As Boolean

    Dim lRowMax As Long
    Dim lRowCnt As Long
    Dim dJyucyuSu As Double
    
    lRowMax = shtLIST.Cells(Rows.Count, 1).End(xlUp).Row
    
    With shtLIST
        For lRowCnt = 2 To lRowMax
            If .Range(GC_DATA_COL_HINSYUKBN & lRowCnt).Value = "5" Then
                '.Range("P" & lRowCnt).Value = .Range("R" & lRowCnt).Value
                '.Range("Q" & lRowCnt).Value = .Range("S" & lRowCnt).Value
                '.Range("AD" & lRowCnt).Value = .Range("AE" & lRowCnt).Value
                .Range(GC_DATA_COL_KAKERITUCD & lRowCnt).Value = .Range(GC_DATA_COL_KAKERITUCD1 & lRowCnt).Value
                .Range(GC_DATA_COL_KAKERITUMEI & lRowCnt).Value = .Range(GC_DATA_COL_KAKERITUMEI2 & lRowCnt).Value
                .Range(GC_DATA_COL_HACYUCD & lRowCnt).Value = .Range(GC_DATA_COL_KAKOUKANSEICD & lRowCnt).Value
            
            
            End If
        Next lRowCnt
    End With
    
            
End Function

