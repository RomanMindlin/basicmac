# CLAUDE.md

Basic MAC: a portable C implementation of LoRaWAN for end devices (Class
A/B/C, multiple regions), forked from IBM LMiC. This checkout is a fork
(github.com/RomanMindlin/basicmac, branch `master`) of mkuyper/basicmac, used
as a submodule of the Tabs firmware repo (`../`). The Tabs apps build their
projects out-of-tree against this tree.

## Layout

- `lmic/` — the MAC core and OS layer: `lmic.c/h` (LoRaWAN state machine),
  `oslmic.c/h` (job scheduler, time, byte-order helpers), `radio-sx127x.c`
  and `radio-sx126x.c`, `region.h`, `lce.c` (crypto), `debug.c`,
  `peripherals.h` (HAL peripheral API), `persodata.c` (EUIs/keys in EEPROM)
- `stm32/` — HAL for STM32L0/L1: `hal.c`, gpio, usart, adc, timer, i2c, dma,
  eeprom, trng, `board.h` + `brd_devboards.h`, `fw.ld`, CMSIS
- `unicorn/` — HAL for the Unicorn-emulated simulation target; `unicorn/simul/`
  is the Python device/network simulator
- `services/` — optional modules, each declared by a `<name>.svc` file:
  `appstart` (provides `main`, calls `hook.appstart`), `lwmux` (uplink
  scheduling/multiplexing, the usual app API), `eefs` (EEPROM file system
  keyed by UFID), `uexti` (shared EXTI dispatch), `pwrman` (energy
  accounting), `perso` (serial personalization/test mode), `eckm` (ECC key
  management), `fuota/` (`frag` fragmentation, `fwman` update management,
  `fuota` FEC), `lwtest` (LoRaWAN certification test mode), `micro-ecc`
  (submodule)
- `projects/` — build system (`projects.gmk`, `platform.mk`, `variants.mk`,
  `fam-*.mk`) and example projects `ex-join`, `ex-fuota`
- `tools/` — `svctool/` (service resolver and `svcdefs.h` generator),
  `openocd/` (flash.cfg, nucleo config, ST-Link udev rules), `fuota/fuotacs.py`
  (FUOTA over ChirpStack v4, fork addition)
- `basicloader/` — Basic Loader bootloader, nested submodule (has its own
  CLAUDE.md)
- `doc/guide/` — Sphinx Getting Started guide

## Build system

A project directory holds a `Makefile`, `main.c` and `app.svc`. The Makefile
sets variables and then includes `projects/projects.gmk`:

- `TARGET` — hyphen-separated families matched in `fam-*.mk` (for example
  `b_l072z_lrwan1`, `nucleo_l073rz-sx1276mb1las`, `unicorn`). Out-of-tree
  projects add their own family file with `FAM_MAKEFILES += ...` (the Tabs
  repo does this for `wsms130`).
- `VARIANTS` — for example `eu868 us915 hybrid simul`. Per-variant overrides
  use `REGIONS.<v>` and `TARGET.<v>`.
- `SVCS` — root services, usually `app`. svctool resolves `require:`
  recursively across the service search path (`-p` dirs, `SVCTOOLARGS`).
- `LMICCFG += X` → `-DCFG_X` (for example `DEBUG`, `extapi`, `eeprom_region`,
  `eeprom_keys`, `EXTI_IRQ_HANDLER=uexti_handler`). `DEFS += -D...` adds raw
  defines.
- Each enabled service defines `SVC_<name>`. Code tests for optional services
  with `#if defined(SVC_xxx)`.

```sh
make -C basicloader             # bootloaders for all boards
cd projects/ex-join
make                            # all variants into build-<variant>/
make VARIANT=eu868              # one variant
make load | loadbl | debug      # OpenOCD/ST-Link (first variant unless VARIANT=)
VARIANT=simul make test         # simulation tests (ward + unicorn)
```

The build pipeline is `.out` → `.unpatched.hex` → `.zfw` (`zfwtool create
--patch` writes the size and CRC into the header) → `.hex` / `.bin`. A fork
addition also produces `.delta.up`, an LZ4 delta update against
`DELTA_REF` (default `ref-<variant>.zfw`), signed with `UPKEY` (default
`services/fuota/testkey.pem`). `make setref` copies the current `.zfw` to the
reference.

CI (`.travis.yml`) builds basicloader, builds ex-join and runs its simul
tests.

## Service files (`.svc`, YAML)

```yaml
src:      [dir/file.c]                         # sources, relative to the service path
require:  [lwmux, eefs]                        # dependencies
hooks:                                         # hooks this service *declares*
    - void lwm_event (ev_t)
    - <first_nz:NULL> const char* eefs_fn (const uint8_t* ufid)
hook.lwm_downlink: my_dl                       # *implement* a hook (optional :priority)
hook.appstart: _perso_main:-10                 # lower priority runs first
define: [CFG_rtstats]                          # extra defines
```

svctool generates `build-*/svcdefs.h`, which declares each hook implementation
and defines `SVCHOOK_<name>(...)`. A hook's style decides how the
implementations are called: all of them (default), `last` (the last return
value wins), or `first_nz` (stop at the first non-zero return).

## Runtime model

- Everything runs from one cooperative run loop (`os_runloop`). Work is
  queued as jobs: `os_setCallback`, `os_setTimedCallback`,
  `os_setApproxTimedCallback`, `os_setExtendedTimedCallback` (64-bit
  `osxtime_t`), cancelled with `os_clearCallback`. An `osjob_t` must be static
  or otherwise long-lived, and can only be queued once at a time.
- Never block or busy-wait. Interrupt handlers do minimal work and schedule a
  job. Protect shared state with `hal_disableIRQs()` / `hal_enableIRQs()`.
- Time: `ostime_t` is 32-bit ticks and wraps, so compare times with
  `(a - b) >= 0`. Convert with `ms2osticks`, `sec2osticks`, `osticks2ms`, and
  so on.
- The MCU sleeps automatically when no job is due. `CFG_rtstats` and
  `pwrman` account for consumption.
- Logging: `debug_printf` (enabled by `CFG_DEBUG`). Format extensions:
  `%E` EUI, `%h` hex dump (pointer, length), `%F` fixed-point (value, decimal
  places).
- Board configuration comes from the header named by `BRD_IMPL_INC` (included
  by `stm32/board.h`). It defines `GPIO_*`, `BRD_*` and radio selection.

## Fork changes (vs. upstream mkuyper/basicmac)

- `projects/projects.gmk` / `platform.mk`: `.delta.up` target, `DELTA_REF`,
  `UPKEY`, `setref`
- `stm32/hal.c`: under `SVC_frag`, `hal_init` sets up fragmentation storage in
  the free flash after the firmware (before `eefs_init`, which restores
  sessions)
- `tools/fuota/`: `fuotacs.py` + README (unicast FUOTA through ChirpStack:
  frag port 201, fwman port 203)
- `basicloader` submodule points to the RomanMindlin fork

Keep fork changes small and contained so merges from upstream stay easy.

## Conventions

- C (gnu11), 4-space indent, `if( cond ) {` spacing, `snake_case`, a
  copyright header on every file.
- Don't change on-air formats, eefs UFIDs, or the bootloader API
  (`basicloader/src/common/bootloader.h`). Devices in the field depend on
  them.
- Python tools need Python ≥ 3.6 (3.8+ recommended). The simulation stack
  needs `unicorn`, `numpy`, `liblora` and `ward` from `requirements.txt`.
