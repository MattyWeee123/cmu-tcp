TOP_DIR = .
INC_DIR = $(TOP_DIR)/inc
SRC_DIR = $(TOP_DIR)/src
BUILD_DIR = $(TOP_DIR)/build
CC=gcc
PYTHON ?= python3
FLAGS = -pthread -fPIC -g -ggdb -pedantic -Wall -Wextra -DDEBUG -I$(INC_DIR)
OBJS = $(BUILD_DIR)/cmu_packet.o $(BUILD_DIR)/cmu_tcp.o $(BUILD_DIR)/backend.o
HEADERS = $(wildcard $(INC_DIR)/*.h)

.PHONY: all format test test-local test-cp2 clean

all: server client tests/testing_server

$(BUILD_DIR)/%.o: $(SRC_DIR)/%.c $(HEADERS) | $(BUILD_DIR)
	$(CC) $(FLAGS) -c -o $@ $<

$(BUILD_DIR):
	mkdir -p $@

server: $(OBJS) $(SRC_DIR)/server.c
	$(CC) $(FLAGS) $(SRC_DIR)/server.c -o server $(OBJS)

client: $(OBJS) $(SRC_DIR)/client.c
	$(CC) $(FLAGS) $(SRC_DIR)/client.c -o client $(OBJS)

tests/testing_server: $(OBJS)
	$(CC) $(FLAGS) tests/testing_server.c -o tests/testing_server $(OBJS)

tests/cp2_transfer: $(OBJS) tests/cp2_transfer.c
	$(CC) $(FLAGS) tests/cp2_transfer.c -o $@ $(OBJS)

format:
	pre-commit run --all-files

test:
	sudo -E python3 tests/test_cp1.py
	sudo -E python3 tests/test_cp1_basic_ack_packets.py

test-local:
	$(PYTHON) tests/test_cp2_local.py

test-cp2: test-local

clean:
	rm -f $(BUILD_DIR)/*.o peer client server
	rm -f tests/testing_server tests/cp2_transfer
