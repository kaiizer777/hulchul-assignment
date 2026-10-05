variable "database_url" {
  type      = string
  sensitive = true
}

variable "groq_api_key" {
  type      = string
  sensitive = true
}

variable "upstash_redis_rest_url" {
  type      = string
  sensitive = true
}

variable "upstash_redis_rest_token" {
  type      = string
  sensitive = true
}

variable "browser_ws_endpoint" {
  type      = string
  sensitive = true
}

variable "auth_password_hash" {
  type      = string
  sensitive = true
}

variable "auth_agent_password" {
  type      = string
  sensitive = true
  default   = ""
}

variable "cors_origins" {
  type    = string
  default = "http://localhost:3051,http://127.0.0.1:3051"
}

variable "frontend_url" {
  type    = string
  default = "http://localhost:3051"
}

variable "image_tag" {
  type    = string
  default = "latest"
}

variable "opencode_api_key" {
  type      = string
  sensitive = true
  default   = ""
}

variable "opencode_base_url" {
  type    = string
  default = "https://opencode.ai/zen/v1"
}

variable "opencode_model" {
  type    = string
  default = "space-bunny-free"
}

variable "erp_base_url" {
  type    = string
  default = "https://hulchul-frontend.sufiyanx.workers.dev"
}

# Optional tuning/threshold/pool overrides. Every one defaults to "" (unset):
# lambda.tf only forwards non-empty values, so the container's code defaults
# in backend/config.py keep applying unless an operator sets these explicitly.
# No values are declared here -- names only.
variable "auth_session_ttl_seconds" {
  type    = string
  default = ""
}

variable "simulate_failure_after" {
  type    = string
  default = ""
}

variable "browser_connect_timeout_ms" {
  type    = string
  default = ""
}

variable "db_pool_min_size" {
  type    = string
  default = ""
}

variable "db_pool_max_size" {
  type    = string
  default = ""
}

variable "db_pool_max_inactive_lifetime" {
  type    = string
  default = ""
}

variable "groq_model" {
  type    = string
  default = ""
}

variable "max_agent_iterations" {
  type    = string
  default = ""
}

variable "default_approval_threshold" {
  type    = string
  default = ""
}

variable "approval_timeout_seconds" {
  type    = string
  default = ""
}

variable "pause_timeout_seconds" {
  type    = string
  default = ""
}

variable "run_lease_seconds" {
  type    = string
  default = ""
}

variable "run_heartbeat_seconds" {
  type    = string
  default = ""
}

variable "enable_api_docs" {
  type    = string
  default = ""
}

