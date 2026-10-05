resource "aws_cloudwatch_log_group" "lambda_log_group" {
  name              = "/aws/lambda/hulchul-backend"
  retention_in_days = 7
}

resource "aws_lambda_function" "backend" {
  function_name     = "hulchul-backend"
  role              = aws_iam_role.lambda_role.arn
  package_type      = "Image"
  image_uri         = "${aws_ecr_repository.backend.repository_url}:${var.image_tag}"
  timeout           = 300
  memory_size       = 1024

  environment {
    # Optional overrides are merged in only when non-empty, so unset values
    # never reach the container as "" (which would break int()/float()
    # parsing in backend/config.py) and the code defaults keep applying.
    variables = merge(
      {
        PORT                       = "8051"
        AWS_LWA_ENABLE_COMPRESSION = "false"
        AWS_LWA_INVOKE_MODE        = "response_stream"
        DATABASE_URL               = var.database_url
        GROQ_API_KEY               = var.groq_api_key
        OPENCODE_API_KEY           = var.opencode_api_key
        OPENCODE_BASE_URL          = var.opencode_base_url
        OPENCODE_MODEL             = var.opencode_model
        UPSTASH_REDIS_REST_URL     = var.upstash_redis_rest_url
        UPSTASH_REDIS_REST_TOKEN   = var.upstash_redis_rest_token
        BROWSER_WS_ENDPOINT        = var.browser_ws_endpoint
        AUTH_PASSWORD_HASH         = var.auth_password_hash
        AUTH_AGENT_PASSWORD        = var.auth_agent_password
        ERP_BASE_URL               = var.erp_base_url
        FRONTEND_URL               = var.frontend_url
        NEXT_PUBLIC_API_URL        = var.frontend_url
        CORS_ORIGINS               = var.cors_origins
      },
      var.auth_session_ttl_seconds != "" ? { AUTH_SESSION_TTL_SECONDS = var.auth_session_ttl_seconds } : {},
      var.simulate_failure_after != "" ? { SIMULATE_FAILURE_AFTER = var.simulate_failure_after } : {},
      var.browser_connect_timeout_ms != "" ? { BROWSER_CONNECT_TIMEOUT_MS = var.browser_connect_timeout_ms } : {},
      var.db_pool_min_size != "" ? { DB_POOL_MIN_SIZE = var.db_pool_min_size } : {},
      var.db_pool_max_size != "" ? { DB_POOL_MAX_SIZE = var.db_pool_max_size } : {},
      var.db_pool_max_inactive_lifetime != "" ? { DB_POOL_MAX_INACTIVE_LIFETIME = var.db_pool_max_inactive_lifetime } : {},
      var.groq_model != "" ? { GROQ_MODEL = var.groq_model } : {},
      var.max_agent_iterations != "" ? { MAX_AGENT_ITERATIONS = var.max_agent_iterations } : {},
      var.default_approval_threshold != "" ? { DEFAULT_APPROVAL_THRESHOLD = var.default_approval_threshold } : {},
      var.approval_timeout_seconds != "" ? { APPROVAL_TIMEOUT_SECONDS = var.approval_timeout_seconds } : {},
      var.pause_timeout_seconds != "" ? { PAUSE_TIMEOUT_SECONDS = var.pause_timeout_seconds } : {},
      var.run_lease_seconds != "" ? { RUN_LEASE_SECONDS = var.run_lease_seconds } : {},
      var.run_heartbeat_seconds != "" ? { RUN_HEARTBEAT_SECONDS = var.run_heartbeat_seconds } : {},
      var.enable_api_docs != "" ? { ENABLE_API_DOCS = var.enable_api_docs } : {},
    )
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

resource "aws_lambda_permission" "public_function_url" {
  statement_id           = "FunctionURLAllowPublicAccess"
  action                 = "lambda:InvokeFunctionUrl"
  function_name          = aws_lambda_function.backend.function_name
  principal              = "*"
  function_url_auth_type = "NONE"
}

resource "aws_lambda_permission" "public_function_invoke" {
  statement_id  = "AllowPublicLambdaFunctionUrl"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.backend.function_name
  principal     = "*"
}
