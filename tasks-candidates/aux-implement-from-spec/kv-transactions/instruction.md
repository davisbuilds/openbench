# In-memory key/value store with transactions

Build a small in-memory key/value store in `store.py`. It reads commands from
standard input, one per line, and writes output to standard output. Keys and
values are single whitespace-free tokens. Unrecognized lines and blank lines are
ignored.

## Commands

| Command          | Effect |
|------------------|--------|
| `SET <k> <v>`    | Set key `<k>` to value `<v>`. No output. |
| `GET <k>`        | Print the current value of `<k>`, or `NULL` if it is not set. |
| `DELETE <k>`     | Remove key `<k>`. No output. |
| `COUNT <v>`      | Print how many keys currently hold the value `<v>`. |
| `BEGIN`          | Open a new transaction block. No output. |
| `ROLLBACK`       | Discard the changes of the innermost open transaction. If no transaction is open, print `NO TRANSACTION`. |
| `COMMIT`         | Make all changes made inside every open transaction permanent and close them all. If no transaction is open, print `NO TRANSACTION`. |
| `END`            | Stop reading input. |

## Transactions

`BEGIN` starts a transaction, and transactions may be nested. Reads (`GET`,
`COUNT`) always reflect the current state, including changes made inside open
transactions.

- `ROLLBACK` undoes only the changes made since the most recent still-open
  `BEGIN`, returning to the state just before it. Transactions opened outside it
  are unaffected.
- `COMMIT` folds the changes from *all* currently open transactions into the
  permanent store at once and leaves no transaction open.

## Example

Input:

```
SET a 1
BEGIN
SET a 2
GET a
BEGIN
DELETE a
GET a
ROLLBACK
GET a
COMMIT
GET a
```

Output:

```
2
NULL
2
2
```

Done when `store.py` implements every command with the transaction semantics
above.
