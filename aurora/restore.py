from .storage import initialize, restore_initial_data


def main() -> None:
    initialize()
    restore_initial_data()
    print("Dados de reservas e visitantes restaurados a partir de dados/.")


if __name__ == "__main__":
    main()
