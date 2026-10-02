resource "aws_cloudwatch_log_group" "lambda_log_group" {
  name              = "/aws/lambda/hulchul-backend"
  retention_in_days = 7
}

resource "aws_lambda_function" "backend" {
  function_name     = "hulchul-backend"
  role              = aws_iam_role.lambda_role.arn
  package_type      = "Image"
  image_uri         = "${aws_ecr_repository.backend.repository_url}:latest"
  timeout           = 300
  memory_size       = 1024

  environment {
    variables = {
      PORT                       = "8051"
      AWS_LWA_ENABLE_COMPRESSION = "false"
      AWS_LWA_INVOKE_MODE        = "response_stream"
      DATABASE_URL               = var.database_url
      GROQ_API_KEY               = var.groq_api_key
      UPSTASH_REDIS_REST_URL     = var.upstash_redis_rest_url
      UPSTASH_REDIS_REST_TOKEN   = var.upstash_redis_rest_token
      BROWSER_WS_ENDPOINT        = var.browser_ws_endpoint
      NEXT_PUBLIC_API_URL        = var.frontend_url
      CORS_ORIGINS               = var.cors_origins
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.lambda_log_group,
    aws_iam_role_policy_attachment.lambda_basic
  ]
}

resource "aws_lambda_function_url" "backend" {
  function_name      = aws_lambda_function.backend.function_name
  authorization_type = "NONE"
  invoke_mode        = "RESPONSE_STREAM"

  cors {
    allow_credentials = true
    allow_origins     = [for origin in split(",", var.cors_origins) : trimspace(origin) if trimspace(origin) != "" && trimspace(origin) != "*"]
    allow_methods     = ["*"]
    allow_headers     = ["*"]
    expose_headers    = ["*"]
    max_age           = 86400
  }
}

resource "aws_lambda_permission" "allow_public_url" {
  statement_id           = "AllowPublicLambdaFunctionUrl"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.backend.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}
