Attribute VB_Name = "mdlCommon"
Option Explicit
'PowerQueryの最終列
Public Const GC_DATA_COL_GLASSKBN       As String = "C" '硝/加区分
'Public Const GC_DATA_COL_HINMEI         As String = "M" '商品名称
'Public Const GC_DATA_COL_JYUCYU         As String = "T" '受注数
'Public Const GC_DATA_COL_HACYUSU        As String = "U" '発注数
'Public Const GC_DATA_COL_HINSYUKBN      As String = "AC" '品種区分
'Public Const GC_DATA_COL_HACYUCD        As String = "AD" '発注先コード
'Public Const GC_DATA_COL_KAKOUHANTEI    As String = "AI" '加工判定 〇:加工, ×:加工以外
'Public Const GC_DATA_COL_SENJYOKBN      As String = "AJ" '洗浄区分 0:不要, 1:洗浄
'Public Const GC_DATA_COL_HANTEI         As String = "AK" '判定 〇:有効でーた, -:不要データ
'Public Const GC_DATA_COL_MAX            As String = "AK"
Public Const GC_DATA_COL_HINMEI         As String = "O" '商品名称

Public Const GC_DATA_COL_KAKERITUCD     As String = "R" '掛率集計コード
Public Const GC_DATA_COL_KAKERITUMEI    As String = "S" '掛率集計名称
Public Const GC_DATA_COL_KAKERITUCD1    As String = "T" '掛率集計コード_1
Public Const GC_DATA_COL_KAKERITUMEI2   As String = "U" '掛率集計名称_2

Public Const GC_DATA_COL_JYUCYU         As String = "V" '受注数
Public Const GC_DATA_COL_HACYUSU        As String = "W" '発注数
Public Const GC_DATA_COL_SOJURYO        As String = "AD" '総重量
Public Const GC_DATA_COL_HINSYUKBN      As String = "AE" '品種区分
Public Const GC_DATA_COL_HACYUCD        As String = "AF" '発注先コード
Public Const GC_DATA_COL_KAKOUKANSEICD  As String = "AG" '加工完成品仕入先コード
Public Const GC_DATA_COL_HACYUCDHANTEI  As String = "AJ" '発注コード_本社判定
Public Const GC_DATA_COL_KAKOUHANTEI    As String = "AK" '加工判定 〇:加工, ×:加工以外
Public Const GC_DATA_COL_SENJYOKBN      As String = "AL" '洗浄区分 0:不要, 1:洗浄
Public Const GC_DATA_COL_HANTEI         As String = "AM" '判定 〇:有効でーた, -:不要データ
Public Const GC_DATA_COL_MAX            As String = "AM"
Public Const GC_DATA_ROW_DATAST         As Long = 2

Public Const GC_GLASSKBN_GLASS          As String = "1" '硝子本体
Public Const GC_GLASSKBN_KAKOU          As String = "2" '加工

Public Sub CopySheet(ByVal wsSourceName As String, ByVal wkTargetName As String)

' 変数の宣言
    Dim wsSource As Worksheet  ' コピー元シート
    Dim wsTarget As Worksheet  ' コピー先シート
    Dim rngSource As Range     ' コピー元範囲
    Dim rngTarget As Range     ' コピー先範囲
    Dim lRowMax As Long
    
    ' コピー元とコピー先のシートを設定
    ' "Sheet1"と"Sheet2"を、実際のシート名に置き換えてください
    Set wsSource = ThisWorkbook.Sheets(wsSourceName)
    Set wsTarget = ThisWorkbook.Sheets(wkTargetName)
    
    lRowMax = wsSource.Cells(Rows.Count, 1).End(xlUp).Row
    
    ' コピー元範囲を設定
    Set rngSource = wsSource.Range("A1:" & GC_DATA_COL_MAX & lRowMax)
    
    ' コピー先範囲の左上隅のセルを設定
    wsTarget.UsedRange.ClearContents
    Set rngTarget = wsTarget.Range("A1")

    ' コピー元範囲のデータをコピー
    rngSource.Copy

    ' 貼り付け先のセルに、値のみを貼り付け
    ' PasteSpecialメソッドの引数にxlPasteValuesを指定することで、値貼り付けを行います
    rngTarget.PasteSpecial Paste:=xlPasteValues

    ' 選択状態を解除（いわゆる「点滅している破線」を消す）
    Application.CutCopyMode = False
    
End Sub
