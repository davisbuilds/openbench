"""Reference solution: spiral matrix."""


def spiral_matrix(size):
    matrix = [[0] * size for _ in range(size)]
    row = col = 0
    d_row, d_col = 0, 1  # start moving right
    for n in range(1, size * size + 1):
        matrix[row][col] = n
        nr, nc = row + d_row, col + d_col
        if not (0 <= nr < size and 0 <= nc < size and matrix[nr][nc] == 0):
            d_row, d_col = d_col, -d_row  # turn clockwise
            nr, nc = row + d_row, col + d_col
        row, col = nr, nc
    return matrix
