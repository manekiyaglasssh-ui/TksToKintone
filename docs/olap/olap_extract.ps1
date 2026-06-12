# olap_extract.ps1
# PowerShell 5.1 以上想定

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $ScriptDir "config.json"
$OrderNoPath = Join-Path $ScriptDir "order_no.txt"

if (-not (Test-Path $ConfigPath)) {
    throw "config.json が見つかりません: $ConfigPath"
}

if (-not (Test-Path $OrderNoPath)) {
    throw "order_no.txt が見つかりません: $OrderNoPath"
}

$config = Get-Content $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json

$orderNos = Get-Content $OrderNoPath -Encoding UTF8 |
    ForEach-Object { $_.Trim() } |
    Where-Object { $_ -ne "" }

if ($orderNos.Count -eq 0) {
    throw "order_no.txt に受注Noがありません。"
}

$orderNoValue = ($orderNos -join ",")

$BaseUrl = $config.BaseUrl.TrimEnd("/")

if ([string]::IsNullOrWhiteSpace($config.Password)) {
    throw "config.json の Password が未設定です。"
}

$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession

function Escape-JsonString {
    param(
        [Parameter(Mandatory = $true)]
        [AllowEmptyString()]
        [string]$Value
    )

    return $Value.Replace("\", "\\").Replace('"', '\"')
}

function ConvertTo-Utf8JsonBytes {
    param(
        [Parameter(Mandatory = $true)]
        [object]$Object,

        [int]$Depth = 100
    )

    $json = $Object | ConvertTo-Json -Depth $Depth -Compress
    return [System.Text.Encoding]::UTF8.GetBytes($json)
}

function Write-Utf8NoBom {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Text
    )

    $encoding = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Text, $encoding)
}

function Show-SessionCookies {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Url,

        [Parameter(Mandatory = $true)]
        [Microsoft.PowerShell.Commands.WebRequestSession]$WebSession
    )

    Write-Host "現在のCookie:"
    $cookies = $WebSession.Cookies.GetCookies($Url)

    if ($cookies.Count -eq 0) {
        Write-Host "  Cookieなし"
        return
    }

    $cookies | ForEach-Object {
        Write-Host ("  {0}={1}" -f $_.Name, $_.Value)
    }
}

Write-Host "ログイン中..."

$companyCode  = Escape-JsonString $config.ContractCompanyCode
$loginId      = Escape-JsonString $config.LoginId
$password     = Escape-JsonString $config.Password
$loginType    = Escape-JsonString $config.LoginAuthType
$deviceId     = Escape-JsonString $config.DeviceId
$computerName = Escape-JsonString $config.ComputerName
$ipAddress    = Escape-JsonString $config.IpAddress

# ログインJSONは元ログと同じ順番で文字列生成する
$loginJson = @"
{"契約会社コード":"$companyCode","ログインID":"$loginId","パスワード":"$password","ログイン認証区分":"$loginType","端末識別ID":"$deviceId","コンピュータ名":"$computerName","IPアドレス":"$ipAddress","ScreenName":0}
"@

$loginBytes = [System.Text.Encoding]::UTF8.GetBytes($loginJson)

$loginWebResponse = Invoke-WebRequest `
    -Uri "$BaseUrl/c/%E3%83%AD%E3%82%B0%E3%82%A4%E3%83%B3%E8%AA%8D%E8%A8%BC" `
    -Method Post `
    -UseBasicParsing `
    -Headers @{
        "Accept" = "application/json"
    } `
    -Body $loginBytes `
    -ContentType "application/json; charset=utf-8" `
    -WebSession $session

$loginResponse = $loginWebResponse.Content | ConvertFrom-Json

Write-Host "ログイン完了"

if ($null -eq $loginResponse.ResponseData) {
    Write-Host "ログインレスポンス:"
    Write-Host $loginWebResponse.Content
    throw "ログインに失敗しました。ResponseData がありません。config.json のログイン情報を確認してください。"
}

if ($loginResponse.ResponseData."Ｘ0" -ne "00") {
    Write-Host "ログインレスポンス:"
    Write-Host $loginWebResponse.Content
    throw "ログインに失敗しました。X0 が 00 ではありません。"
}

Write-Host ("会社名: {0}" -f $loginResponse.ResponseData."Ｘ3")

Show-SessionCookies -Url $BaseUrl -WebSession $session

$authCookie = $session.Cookies.GetCookies($BaseUrl) | Where-Object { $_.Name -eq ".ASPXAUTH" }

if ($null -eq $authCookie) {
    throw "ログインCookie .ASPXAUTH が取得できていません。"
}

$target = "OLAP_T01-03 受注入力明細データ"

function New-OlapColumn {
    param(
        [int]$No,
        [string]$DisplayName,
        [string]$FieldName,
        [string]$DataType,
        [int]$Width,
        [int]$Digits,
        [int]$Decimals,
        [string]$SummaryMethod,
        [AllowEmptyString()]
        [string]$FormulaText,
        [string]$DomainType
    )

    return [ordered]@{
        "OLAP表示No" = $No
        "OLAP表示名" = $DisplayName
        "OLAPデータ区分" = $DataType
        "エンティティ論理名" = $target
        "フィールド論理名" = $FieldName
        "OLAP表示幅" = $Width
        "OLAPフォントサイズ２" = "0"
        "OLAP空白値表示" = "-"
        "OLAP日付のフォーマットフラグ" = "1"
        "OLAP数値の3桁区切りフラグ" = "1"
        "OLAP桁数" = $Digits
        "OLAP小数" = $Decimals
        "OLAP丸め" = "0"
        "OLAP出力順序No" = $null
        "OLAP出力順" = "2"
        "OLAP空白値を先頭表示フラグ" = "0"
        "OLAP集計方法" = $SummaryMethod
        "OLAP合計表示フラグ" = "0"
        "OLAP合計ラベル" = "計"
        "OLAP合計ラベルのみ表示フラグ" = $null
        "OLAP重複を除くフラグ" = "0"
        "OLAP演算式" = $null
        "OLAP演算式表記" = $FormulaText
        "OLAPドメイン分類" = $DomainType
        "XupperRoutingItems" = @()
    }
}

function New-OlapCondition {
    param(
        [int]$No,
        [string]$FieldName,
        [string]$Value,
        [string]$DomainType
    )

    return [ordered]@{
        "OLAP表示No" = $No
        "OLAP一致指定フラグ" = "1"
        "OLAP一致指定" = "0"
        "OLAP除外指定フラグ" = "0"
        "OLAP値" = $Value
        "OLAP範囲指定フラグ" = "0"
        "OLAP範囲_Fromフラグ" = "1"
        "OLAP範囲Val_From" = ""
        "OLAP範囲Sel_From" = "0"
        "OLAP範囲_Toフラグ" = "1"
        "OLAP範囲Val_To" = ""
        "OLAP範囲Sel_To" = "0"
        "OLAP月度指定フラグ" = "0"
        "OLAP月度指定" = "0"
        "OLAP条件グループ" = "0"
        "OLAP空白" = "1"
        "OLAPドメイン分類" = $DomainType
        "エンティティ論理名" = $target
        "フィールド論理名" = $FieldName
        "XupperRoutingItems" = @()
    }
}

$r1List = @(
    New-OlapColumn 1  "受注No"       "受注No"       "1" 8  0 0 "0" ""  "0"
    New-OlapColumn 2  "受注行No"     "受注行No"     "2" 4  3 0 "3" "1" "1"
    New-OlapColumn 3  "得意先コード" "得意先コード" "1" 7  0 0 "0" ""  "0"
    New-OlapColumn 4  "得意先名称"   "得意先名称"   "1" 30 0 0 "0" ""  "0"
    New-OlapColumn 5  "納品書No"     "納品書No"     "1" 8  0 0 "0" ""  "0"
    New-OlapColumn 6  "納品書行No"   "納品書行No"   "2" 4  3 0 "3" "2" "1"
    New-OlapColumn 7  "納品日"       "納品日"       "1" 10 0 0 "0" ""  "2"
    New-OlapColumn 8  "売上日"       "売上計上日"   "1" 10 0 0 "0" ""  "2"
    New-OlapColumn 9  "発注日"       "発注日"       "1" 10 0 0 "0" ""  "2"
    New-OlapColumn 10 "入庫日"       "入庫日"       "1" 10 0 0 "0" ""  "2"
)

$r2List = @(
    New-OlapCondition 1 "営業所コード"   "010,040"     "0"
    New-OlapCondition 2 "納品書発行区分" "1"           "3"
    New-OlapCondition 3 "受注No"         $orderNoValue "0"
)

# OLAPデータ側も元ログのトップレベル順に合わせる
$olapBody = [ordered]@{
    "OLAP出力レイアウト" = "0"
    "OLAP対象データ" = $target
    "R1List" = $r1List
    "R2List" = $r2List
    "ScreenName" = 0
}

Write-Host "OLAPデータ抽出中..."
Write-Host "受注No: $orderNoValue"

Show-SessionCookies -Url $BaseUrl -WebSession $session

$debugOlapJsonPath = Join-Path $ScriptDir "debug_olap_request.json"
$debugOlapJson = $olapBody | ConvertTo-Json -Depth 100 -Compress
Write-Utf8NoBom -Path $debugOlapJsonPath -Text $debugOlapJson
Write-Host "送信OLAP JSON: $debugOlapJsonPath"

$olapBytes = [System.Text.Encoding]::UTF8.GetBytes($debugOlapJson)

$olapWebResponse = Invoke-WebRequest `
    -Uri "$BaseUrl/c/OLAP%E3%83%87%E3%83%BC%E3%82%BF" `
    -Method Put `
    -UseBasicParsing `
    -Headers @{
        "Accept" = "application/json"
    } `
    -Body $olapBytes `
    -ContentType "application/json; charset=utf-8" `
    -WebSession $session

$result = $olapWebResponse.Content | ConvertFrom-Json

$jsonPath = Join-Path $ScriptDir $config.OutputJson
$csvPath = Join-Path $ScriptDir $config.OutputCsv

$resultJson = $result | ConvertTo-Json -Depth 100
Write-Utf8NoBom -Path $jsonPath -Text $resultJson

if ($null -eq $result.ResponseData) {
    Write-Host "OLAPレスポンス:"
    Write-Host $olapWebResponse.Content
    throw "OLAPデータ抽出に失敗しました。ResponseData がありません。debug_olap_request.json と元ログのPUT本文を比較してください。"
}

$rowsObject = $result.ResponseData.R1List
$csvRows = @()

if ($null -ne $rowsObject) {
    $properties = $rowsObject.PSObject.Properties | Sort-Object { [int]$_.Name }

    foreach ($prop in $properties) {
        $row = $prop.Value

        $csvRows += [PSCustomObject]@{
            "受注No"       = $row."1"
            "受注行No"     = $row."2"
            "得意先コード" = $row."3"
            "得意先名称"   = $row."4"
            "納品書No"     = $row."5"
            "納品書行No"   = $row."6"
            "納品日"       = $row."7"
            "売上日"       = $row."8"
            "発注日"       = $row."9"
            "入庫日"       = $row."10"
        }
    }
}

$csvRows | Export-Csv $csvPath -NoTypeInformation -Encoding UTF8

Write-Host "完了しました。"
Write-Host "件数: $($csvRows.Count)"
Write-Host "JSON: $jsonPath"
Write-Host "CSV : $csvPath"