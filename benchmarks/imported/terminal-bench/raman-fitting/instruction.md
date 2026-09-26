You are given the output file of a Raman setup, `graphene.dat`, in the working
directory. It was used to measure a graphene sample.

Fit the G and 2D peaks of the spectrum and write the fitted peak parameters to
`results.json` in the working directory. The required parameters are `x0`,
`gamma`, `amplitude`, and `offset` for each peak.

The file should have the following format:

```json
{
  "G": {
    "x0": <x0_value>,
    "gamma": <gamma_value>,
    "amplitude": <amplitude_value>,
    "offset": <offset_value>
  },
  "2D": {
    "x0": <x0_value>,
    "gamma": <gamma_value>,
    "amplitude": <amplitude_value>,
    "offset": <offset_value>
  }
}
```
