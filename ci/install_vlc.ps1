# Install 64-bit VLC for the Windows test jobs, straight from VideoLAN.
#
#   ci/install_vlc.ps1
#
# These jobs used to run `choco install vlc`. Chocolatey's community feed
# answered 503 on 2026-10-19 and again on 2026-10-22, failing runs that had
# nothing wrong with them, and choco has also exited 0 having "installed
# 0/0 packages" - which only showed later, as the smoke test's "SELFTEST:
# not ready - VLC not found".
#
# So: VideoLAN's own installer, at a pinned version, checked against the
# SHA-256 that VideoLAN publishes for it. get.videolan.org hands each
# request to one of its mirrors, so a download that fails, or arrives with
# the wrong bytes, is asked for again, usually from another mirror. And
# nothing counts as installed until the app's own check finds libvlc
# (depcheck.vlc_dll_path) and python-vlc loads it, so a job that gets past
# this step has a VLC its tests can use.
#
# To move to another VLC, change both lines below. The checksum is in
# vlc-<version>-win64.exe.sha256, beside the installer under
# https://download.videolan.org/pub/videolan/vlc/<version>/win64/

$Version = '3.0.24'
$Sha256 = 'd711e1e1fe52052748c39080c7dce63f6b7e4c315efedf1774a3f1957b782ff3'

$ErrorActionPreference = 'Stop'
$url = "https://get.videolan.org/vlc/$Version/win64/vlc-$Version-win64.exe"
$exe = Join-Path $env:RUNNER_TEMP "vlc-$Version-win64.exe"
$tries = 4

for ($try = 1; ; $try++) {
    curl.exe --fail --silent --show-error --location --connect-timeout 30 `
        --max-time 600 --output $exe $url
    if ($LASTEXITCODE -eq 0) {
        $got = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($got -eq $Sha256) { break }
        Write-Host "Download $try of ${tries}: SHA-256 $got, not the pinned $Sha256."
    } else {
        Write-Host "Download $try of ${tries} failed: curl exited $LASTEXITCODE."
    }
    if ($try -ge $tries) {
        throw "No good copy of VLC $Version from VideoLAN in $tries tries."
    }
    Start-Sleep -Seconds (15 * $try)
}
Write-Host "Downloaded vlc-$Version-win64.exe, and its SHA-256 matches."

$p = Start-Process $exe -ArgumentList '/S' -Wait -PassThru
if ($p.ExitCode -ne 0) { throw "VLC's installer exited $($p.ExitCode)." }

# The app's own test for a usable VLC, then the library python-vlc loads.
@"
import sys
import depcheck
dll = depcheck.vlc_dll_path()
if not dll:
    sys.exit('VLC $Version installed, but the app does not find its libvlc.dll.')
import vlc
got = vlc.libvlc_get_version().decode()
print(f'VLC {got} at {dll}')
if not got.startswith('$Version '):
    sys.exit('Installed VLC $Version, but python-vlc loaded ' + got)
"@ | python -
if ($LASTEXITCODE -ne 0) { throw "VLC $Version did not install in a form the tests can use." }
