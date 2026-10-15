# Downloads cuBLAS and cuDNN from NVIDIA's redistributable CDN, checks them
# against NVIDIA's published SHA-256, and unpacks them into cuda\. aziz_write.py
# loads the DLLs from cuda\*\bin.
$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$downloads = Join-Path $root 'cuda-download'
$target = Join-Path $root 'cuda'
$base = 'https://developer.download.nvidia.com/compute'
$archives = @(
    @{ Path = 'cudnn/redist/cudnn/windows-x86_64/cudnn-windows-x86_64-9.14.0.64_cuda12-archive.zip'
       Sha256 = '27db1cf50f830a1335de991cf1edde43afb4fc65c365dbf013245f1364ec2b12' },
    @{ Path = 'cuda/redist/libcublas/windows-x86_64/libcublas-windows-x86_64-12.9.1.4-archive.zip'
       Sha256 = 'd534d98b0b453a98914dbf3adf47d7e84b55037abf02f87466439e1dcef581ed' }
)
New-Item -ItemType Directory -Force $downloads, $target | Out-Null
foreach ($a in $archives) {
    $zip = Join-Path $downloads (Split-Path $a.Path -Leaf)
    if (-not (Test-Path $zip)) { curl.exe -L --fail -o $zip "$base/$($a.Path)" }
    if ((Get-FileHash $zip -Algorithm SHA256).Hash -ne $a.Sha256) { throw "checksum mismatch: $zip" }
    Expand-Archive $zip -DestinationPath $target -Force
}
Get-ChildItem $target -Directory | ForEach-Object { "unpacked $($_.Name)" }
