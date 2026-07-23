# 자체 서명 코드서명 인증서 생성(없으면) + 공개 인증서(.cer) 내보내기
param([string]$CerOut)
$ErrorActionPreference = 'Stop'
$subj = 'CN=ScrollCapture'
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq $subj -and $_.HasPrivateKey } |
    Select-Object -First 1
if (-not $cert) {
    $cert = New-SelfSignedCertificate -Type CodeSigningCert -Subject $subj `
        -CertStoreLocation Cert:\CurrentUser\My -KeyUsage DigitalSignature `
        -KeyExportPolicy Exportable -FriendlyName 'ScrollCapture Code Signing' `
        -NotAfter (Get-Date).AddYears(10)
    Write-Output "CREATED $($cert.Thumbprint)"
} else {
    Write-Output "EXISTS $($cert.Thumbprint)"
}
if ($CerOut) {
    $dir = Split-Path $CerOut
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Export-Certificate -Cert $cert -FilePath $CerOut -Force | Out-Null
    Write-Output "EXPORTED $CerOut"
}
