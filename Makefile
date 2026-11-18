# Convenience targets. Works on the Mac and on the Pi.
#   make test      build and run unit tests
#   make profile   generate test signals and profile every preset
#   make webapp    run the web app (fake engine) on PORT, default 8080
#   make webapp-test  web app tests (needs fxrender, so builds first)

BUILD ?= build
PORT ?= 8080

.PHONY: build test check signals profile webapp webapp-test clean

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

webapp: build
	uv run --extra webapp fx-web --port $(PORT) --fxrender $(BUILD)/fxrender

webapp-test: build
	FX_FXRENDER=$(BUILD)/fxrender uv run --extra webapp pytest webapp/tests -q

clean:
	rm -rf $(BUILD)
