# 지정한 파일을 코드서명(타임스탬프 포함). 관리자 권한 불필요.
param([Parameter(Mandatory=$true)][string]$File)
$ErrorActionPreference = 'Stop'
$subj = 'CN=ScrollCapture'
$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { $_.Subject -eq $subj -and $_.HasPrivateKey } |
    Select-Object -First 1
if (-not $cert) { Write-Output 'NOCERT'; exit 1 }

$servers = @(
    'http://timestamp.digicert.com',
    'http://timestamp.sectigo.com',
    'http://time.certum.pl'
)
foreach ($ts in $servers) {
    try {
        $r = Set-AuthenticodeSignature -FilePath $File -Certificate $cert `
             -HashAlgorithm SHA256 -TimestampServer $ts -ErrorAction Stop
        Write-Output "SIGNED [$($r.Status)] via $ts : $File"
        exit 0
    } catch {
        Write-Output "TS-FAIL $ts"
    }
}
# 타임스탬프 실패 시 타임스탬프 없이 서명
$r = Set-AuthenticodeSignature -FilePath $File -Certificate $cert -HashAlgorithm SHA256
Write-Output "SIGNED-NOTS [$($r.Status)] : $File"
