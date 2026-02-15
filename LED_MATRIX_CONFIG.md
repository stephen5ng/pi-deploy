# LED Matrix Configuration

## Panel Specifications

**Panel Type:** AliExpress 128×64 RGB LED panels (ABC-addressed)
- **Per Panel:** 128 columns × 64 rows
- **Panel Pitch:** P10 (estimated)
- **Addressing:** ABC-addressed (requires `--led-row-addr-type=3`)

## Working Configurations

### Configuration 1: 3 Parallel Chains (3×2 Panel Grid)
**Layout:** 3 rows, 2 panels per row
- **Total Display:** 256 columns × 192 rows
- **Parameters:**
  ```bash
  --led-cols=128
  --led-rows=64
  --led-chain=2
  --led-parallel=3
  --led-row-addr-type=3
  --led-slowdown-gpio=3
  --led-scan-mode=1
  ```

**Example:**
```bash
sudo /opt/rpi-rgb-led-matrix/examples-api-use/demo -D 0 \
  --led-cols=128 --led-rows=64 \
  --led-chain=2 --led-parallel=3 \
  --led-row-addr-type=3 \
  --led-slowdown-gpio=3 \
  --led-scan-mode=1
```

### Configuration 2: 2 Chained Panels (Horizontal)
**Layout:** Single row, 2 panels chained horizontally
- **Total Display:** 256 columns × 64 rows
- **Parameters:**
  ```bash
  --led-cols=128
  --led-rows=64
  --led-chain=2
  --led-row-addr-type=3
  --led-slowdown-gpio=5
  ```

**Example:**
```bash
sudo /opt/rpi-rgb-led-matrix/examples-api-use/demo -D 0 \
  --led-cols=128 --led-rows=64 \
  --led-chain=2 \
  --led-row-addr-type=3 \
  --led-slowdown-gpio=5
```

## Key Parameters Explained

- **`--led-cols`**: Width of each individual panel (128 for these panels)
- **`--led-rows`**: Height of each individual panel (64 for these panels)
- **`--led-chain`**: Number of panels daisy-chained horizontally
- **`--led-parallel`**: Number of parallel chains stacked vertically
- **`--led-row-addr-type`**: Addressing type (3 = ABC-addressed for these panels)
- **`--led-slowdown-gpio`**: GPIO timing slowdown (higher = slower, more stable for sensitive panels)
- **`--led-scan-mode`**: 0 = progressive, 1 = interlaced

## Troubleshooting Notes

### Streaking/Ghosting
- Increase `--led-slowdown-gpio` value (tested: 3 and 5 work well)
- Try adjusting `--led-scan-mode` (0 vs 1)

### Display Mapping Issues
- GPIO timing is critical for chaining. These panels are sensitive to timing.
- 2-panel chain requires higher slowdown (`--led-slowdown-gpio=5`) than 3-parallel setup (`--led-slowdown-gpio=3`)
- Do NOT use the same slowdown value for different configurations without testing

### Cube Positioning Issues
- If cube appears on wrong panel or only half-visible, adjust slowdown-gpio or scan-mode
- Test incrementally (slowdown values from 1-5)

## Demo Binaries

Located at: `/opt/rpi-rgb-led-matrix/examples-api-use/`

Available demos (use `-D <number>`):
- 0 - Rotating square
- 1 - Forward scrolling image
- 2 - Backward scrolling image
- 3 - Test square
- 4 - Pulsing color
- 5 - Grayscale block
- 6 - Abelian sandpile
- 7 - Conway's game of life
- 8 - Langton's ant
- 9 - Volume bars
- 10 - Color evolution
- 11 - Brightness pulse
- 12 - 3D rotating cube
