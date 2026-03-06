.PHONY: build test lint clean deps

# Build all binaries
build: deps
	go build -o bin/app ./cmd/app
	go build -o bin/backtest ./cmd/backtest
	go build -o bin/worker ./cmd/worker

# Run tests
test:
	go test ./...

# Run linter
lint:
	golangci-lint run ./...

# Download dependencies
deps:
	go mod tidy
	go mod download

# Clean build artifacts
clean:
	rm -rf bin/
