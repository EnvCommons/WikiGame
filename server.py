from openreward.environments import Server

from wikigame import WikiGame

if __name__ == "__main__":
    server = Server([WikiGame])
    server.run()
