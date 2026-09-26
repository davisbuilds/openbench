# Roman numeral converter

Convert a positive integer (1 through 3999) to its Roman numeral string.

Roman numerals combine these symbols, largest value first: `M`=1000, `D`=500,
`C`=100, `L`=50, `X`=10, `V`=5, `I`=1. A symbol may repeat up to three times to
add its value. Instead of four repeats, subtractive pairs are used: `IV`=4,
`IX`=9, `XL`=40, `XC`=90, `CD`=400, `CM`=900.

Build the numeral by taking as many of each value as fit, high to low, using the
subtractive pairs where they apply. Examples: 4 → `IV`, 14 → `XIV`, 49 → `XLIX`,
1990 → `MCMXC`, 2008 → `MMVIII`, 3888 → `MMMDCCCLXXXVIII`.

## Interface

Put your solution in a file named `solution.py` in this directory, exposing:

- `roman(number)` — return the Roman numeral for `number`

Done when `solution.py` implements the interface above exactly as specified.
