$ErrorActionPreference = "Stop"

$env:GODEBUG = 'netdns=cgo'

Write-Host "Reading backend/.env..." -ForegroundColor Cyan
if (!(Test-Path "../../backend/.env")) {
    Write-Error "backend/.env not found!"
    exit 1
}

$envContent = Get-Content "../../backend/.env"
$tfvarsPath = "terraform.tfvars"
$tfvarsLines = @()

foreach ($line in $envContent) {
    if ($line -match "^\s*#") { continue }
    if ($line -match "^\s*$") { continue }
    
    $parts = $line -split "=", 2
    if ($parts.Length -eq 2) {
        $key = $parts[0].Trim().ToLower()
        $val = $parts[1].Trim().Trim('"').Trim("'")
        
        if ($key -eq "database_url") { $tfvarsLines += "database_url = `"$val`"" }
        elseif ($key -eq "groq_api_key") { $tfvarsLines += "groq_api_key = `"$val`"" }
        elseif ($key -eq "upstash_redis_rest_url") { $tfvarsLines += "upstash_redis_rest_url = `"$val`"" }
        elseif ($key -eq "upstash_redis_rest_token") { $tfvarsLines += "upstash_redis_rest_token = `"$val`"" }
        elseif ($key -eq "browser_ws_endpoint") { $tfvarsLines += "browser_ws_endpoint = `"$val`"" }
        elseif ($key -eq "next_public_api_url") { $tfvarsLines += "frontend_url = `"$val`"" }
        elseif ($key -eq "cors_origins") { $tfvarsLines += "cors_origins = `"$val`"" }
    }
}

if (!($tfvarsLines -match "cors_origins")) {
    $tfvarsLines += 'cors_origins = "http://localhost:3051,http://127.0.0.1:3051,https://*.pages.dev,*"'
}

$tfvarsLines | Out-File -Encoding utf8 $tfvarsPath
Write-Host "Generated terraform.tfvars successfully." -ForegroundColor Green

Write-Host "Initializing Terraform..." -ForegroundColor Cyan
& 'C:\Terraform\terraform.exe' init

Write-Host "Applying Terraform to create ECR repository..." -ForegroundColor Cyan
& 'C:\Terraform\terraform.exe' apply -target="aws_ecr_repository.backend" -auto-approve

$ecrUrl = ((& 'C:\Terraform\terraform.exe' output -raw ecr_repository_url) -replace "`r","" -replace "`n","").Trim()
Write-Host "ECR Repository URL: $ecrUrl" -ForegroundColor Green

if ([string]::IsNullOrWhiteSpace($ecrUrl)) {
    Write-Error "Failed to retrieve ECR repository URL from Terraform output!"
    exit 1
}

Write-Host "Logging into AWS ECR..." -ForegroundColor Cyan
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin $ecrUrl

Write-Host "Building Docker image..." -ForegroundColor Cyan
docker build -t hulchul-backend -f ../../backend/Dockerfile ../../backend

Write-Host "Tagging Docker image..." -ForegroundColor Cyan
docker tag hulchul-backend:latest "${ecrUrl}:latest"

Write-Host "Pushing Docker image to ECR..." -ForegroundColor Cyan
docker push "${ecrUrl}:latest"

Write-Host "Applying remaining Terraform infrastructure..." -ForegroundColor Cyan
& 'C:\Terraform\terraform.exe' apply -auto-approve

Write-Host "Deployment completed successfully!" -ForegroundColor Green
