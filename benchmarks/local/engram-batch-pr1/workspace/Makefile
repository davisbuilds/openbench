BINARY := engram
BIN_DIR := bin

.PHONY: build test lint fmt vet clean

build:
	go build -o $(BIN_DIR)/$(BINARY) ./cmd/engram

test:
	go test ./...

vet:
	go vet ./...

lint:
	golangci-lint run

fmt:
	gofumpt -w .

clean:
	rm -rf $(BIN_DIR)
