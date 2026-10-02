output "ecr_repository_url" {
  value = aws_ecr_repository.backend.repository_url
}

output "lambda_function_name" {
  value = aws_lambda_function.backend.function_name
}

output "backend_function_url" {
  value = aws_lambda_function_url.backend.function_url
}
