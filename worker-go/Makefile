GO ?= /opt/homebrew/bin/go
BIN := bin/worker

.PHONY: build run tidy clean

build:
	$(GO) build -o $(BIN) .

run: build
	./$(BIN)

tidy:
	$(GO) mod tidy

clean:
	rm -f $(BIN)
