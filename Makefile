# Convenience targets. Works on the Mac and on the Pi.
#   make test      build and run unit tests
#   make profile   generate test signals and profile every preset

BUILD ?= build

.PHONY: build test check signals profile clean

build:
	cmake -S . -B $(BUILD)
	cmake --build $(BUILD) -j

test: build
	ctest --test-dir $(BUILD) --output-on-failure

check: build
	./$(BUILD)/fxrender --check presets/*.json

signals:
	uv run fx-signals

profile: build signals
	uv run fx-profile

clean:
	rm -rf $(BUILD)
