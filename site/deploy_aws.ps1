# Deploy the StreamSync landing page to an S3 static website.
# Prereq: run `aws configure` once with your AWS access key (2 minutes).
# Usage:  powershell -File deploy_aws.ps1            (random bucket name)
#         powershell -File deploy_aws.ps1 -Bucket my-name -Region us-east-1

param(
    [string]$Bucket = "streamsync-landing-$(Get-Random -Maximum 99999999)",
    [string]$Region = "us-east-1"
)

$ErrorActionPreference = "Stop"

Write-Output "Creating bucket $Bucket in $Region..."
if ($Region -eq "us-east-1") {
    aws s3api create-bucket --bucket $Bucket --region $Region | Out-Null
} else {
    aws s3api create-bucket --bucket $Bucket --region $Region `
        --create-bucket-configuration LocationConstraint=$Region | Out-Null
}

Write-Output "Allowing public reads (this bucket only)..."
aws s3api put-public-access-block --bucket $Bucket --public-access-block-configuration `
    BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false

$policyFile = Join-Path $env:TEMP "streamsync-bucket-policy.json"
@"
{"Version":"2012-10-17","Statement":[{"Sid":"PublicRead","Effect":"Allow","Principal":"*","Action":"s3:GetObject","Resource":"arn:aws:s3:::$Bucket/*"}]}
"@ | Out-File -Encoding ascii $policyFile
aws s3api put-bucket-policy --bucket $Bucket --policy "file://$policyFile"

$siteFile = Join-Path $env:TEMP "streamsync-website.json"
'{"IndexDocument":{"Suffix":"index.html"}}' | Out-File -Encoding ascii $siteFile
aws s3api put-bucket-website --bucket $Bucket --website-configuration "file://$siteFile"

Write-Output "Uploading index.html..."
aws s3 cp "$PSScriptRoot\index.html" "s3://$Bucket/index.html" `
    --content-type "text/html; charset=utf-8" --cache-control "max-age=300"

Write-Output ""
Write-Output "LIVE:  http://$Bucket.s3-website-$Region.amazonaws.com"
Write-Output "(HTTP only - add CloudFront in front of it for HTTPS and a custom domain.)"
