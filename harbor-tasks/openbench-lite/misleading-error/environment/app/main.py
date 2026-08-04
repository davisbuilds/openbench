from app.report import compute_total

COUNT = 10


def main():
    total = compute_total(COUNT)
    print("Total: {}".format(total))


if __name__ == "__main__":
    main()
