$ErrorActionPreference = "Stop"

$PSScriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $PSScriptRoot

$env:GODEBUG = 'netdns=cgo'

function Assert-LastExitCode($cmd) {
    if ($LASTEXITCODE -ne 0) {
        throw "Command '$cmd' failed with exit code $LASTEXITCODE"
    }
}

Write-Host "Locating Terraform binary..." -ForegroundColor Cyan
$terraformPath = "C:\Terraform\terraform.exe"
if (!(Test-Path $terraformPath)) {
    $found = (Get-Command terraform -ErrorAction SilentlyContinue).Source
    if ($found) {
        $terraformPath = $found
    } else {
        throw "Terraform executable not found at C:\Terraform\terraform.exe or in PATH."
    }
}
Write-Host "Using Terraform at: $terraformPath" -ForegroundColor Green

Write-Host "Reading backend/.env..." -ForegroundColor Cyan
$envPath = Join-Path $PSScriptRoot "../../backend/.env"
if (!(Test-Path $envPath)) {
    Write-Error "backend/.env not found at $envPath!"
    exit 1
}

$envContent = Get-Content $envPath
$envDict = @{}

foreach ($line in $envContent) {
    if ($line -match "^\s*#") { continue }
    if ($line -match "^\s*$") { continue }
    
    $parts = $line -split "=", 2
    if ($parts.Length -eq 2) {
        $key = $parts[0].Trim().ToLower()
        $val = $parts[1].Trim().Trim('"').Trim("'")
        $envDict[$key] = $val
    }
}

$corsOrigins = if ($envDict.ContainsKey("cors_origins") -and $envDict["cors_origins"]) {
    $envDict["cors_origins"]
} else {
    "http://localhost:3051,http://127.0.0.1:3051,https://*"
}

$tfvarsObj = @{
    database_url             = $envDict["database_url"]
    groq_api_key             = $envDict["groq_api_key"]
    upstash_redis_rest_url   = $envDict["upstash_redis_rest_url"]
    upstash_redis_rest_token = $envDict["upstash_redis_rest_token"]
    browser_ws_endpoint      = $envDict["browser_ws_endpoint"]
    frontend_url             = $envDict["next_public_api_url"]
    cors_origins             = $corsOrigins
}

$tfvarsJsonPath = "terraform.tfvars.json"
$jsonContent = $tfvarsObj | ConvertTo-Json -Depth 10
[System.IO.File]::WriteAllText((Join-Path (Get-Location) $tfvarsJsonPath), $jsonContent, (New-Object System.Text.UTF8Encoding $false))
Write-Host "Generated terraform.tfvars.json successfully." -ForegroundColor Green

Write-Host "Initializing Terraform..." -ForegroundColor Cyan
& $terraformPath init
Assert-LastExitCode "terraform init"

Write-Host "Applying Terraform to create ECR repository..." -ForegroundColor Cyan
& $terraformPath apply -target aws_ecr_repository.backend -auto-approve
Assert-LastExitCode "terraform apply -target aws_ecr_repository.backend"

$ecrUrl = & $terraformPath output -raw ecr_repository_url
Assert-LastExitCode "terraform output -raw ecr_repository_url"
$ecrDomain = $ecrUrl.Trim()
Write-Host "ECR Repository URL: $ecrDomain" -ForegroundColor Green

if ([string]::IsNullOrWhiteSpace($ecrDomain)) {
    Write-Error "Failed to retrieve ECR repository URL from Terraform output!"
    exit 1
}

Write-Host "Logging into AWS ECR..." -ForegroundColor Cyan
$pass = aws ecr get-login-password --region us-east-1
if ($LASTEXITCODE -ne 0) { throw "aws ecr get-login-password failed with exit code $LASTEXITCODE" }

$registryHost = $ecrDomain.Split('/')[0]
$pass | docker login --username AWS --password-stdin $registryHost
if ($LASTEXITCODE -ne 0) { throw "docker login failed with exit code $LASTEXITCODE" }

$imageTag = (Get-Date -Format "yyyyMMddHHmmss")
Write-Host "Building Docker image with tag $imageTag (provenance=false)..." -ForegroundColor Cyan
docker build --provenance=false -t hulchul-backend -f ../../backend/Dockerfile ../../backend
Assert-LastExitCode "docker build"

Write-Host "Tagging Docker image..." -ForegroundColor Cyan
docker tag hulchul-backend:latest "${ecrDomain}:${imageTag}"
Assert-LastExitCode "docker tag with timestamp"

docker tag hulchul-backend:latest "${ecrDomain}:latest"
Assert-LastExitCode "docker tag latest"

Write-Host "Pushing Docker image to ECR..." -ForegroundColor Cyan
docker push "${ecrDomain}:${imageTag}"
Assert-LastExitCode "docker push timestamp"

docker push "${ecrDomain}:latest"
Assert-LastExitCode "docker push latest"

Write-Host "Cleaning up any stale AWS Lambda permissions..." -ForegroundColor Cyan
aws lambda remove-permission --function-name hulchul-backend --statement-id FunctionURLAllowPublicAccess 2>$null
aws lambda remove-permission --function-name hulchul-backend --statement-id AllowPublicLambdaFunctionUrl 2>$null

Write-Host "Applying remaining Terraform infrastructure with image_tag=$imageTag..." -ForegroundColor Cyan
& $terraformPath apply -var="image_tag=$imageTag" -auto-approve
Assert-LastExitCode "terraform apply"

$backendFunctionUrl = & $terraformPath output -raw backend_function_url
Assert-LastExitCode "terraform output -raw backend_function_url"
Write-Host "Deployment completed successfully! Backend Function URL: $backendFunctionUrl" -ForegroundColor Green
