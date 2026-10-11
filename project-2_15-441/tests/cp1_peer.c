/* Small application for the portable CP1 tests. Protocol logic stays in src/.
 */
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "cmu_tcp.h"

int main(int argc, char** argv) {
  if (argc != 8) return 2;
  cmu_socket_t sock;
  cmu_socket_type_t type =
      strcmp(argv[1], "listener") == 0 ? TCP_LISTENER : TCP_INITIATOR;
  if (cmu_socket(&sock, type, atoi(argv[2]), "127.0.0.1") < 0) {
    perror("cmu_socket");
    return 1;
  }
  printf("READY %u\n", sock.my_port);
  fflush(stdout);

  unsigned char buffer[4096];
  if (cmu_write(&sock, NULL, 0) != 0 ||
      cmu_read(&sock, NULL, 0, NO_FLAG) != 0 ||
      cmu_read(&sock, NULL, 0, NO_WAIT) != 0 ||
      cmu_write(&sock, buffer, -1) != -1 ||
      cmu_read(&sock, buffer, -1, NO_WAIT) != -1)
    return 3;

  if (strcmp(argv[3], "-") != 0) {
    FILE* input = fopen(argv[3], "rb");
    if (input == NULL) return 4;
    size_t length;
    while ((length = fread(buffer, 1, sizeof(buffer), input)) > 0) {
      if (cmu_write(&sock, buffer, (int)length) < 0) return 5;
    }
    if (ferror(input)) return 6;
    fclose(input);
  }
  FILE* output = fopen(argv[4], "wb");
  if (output == NULL) return 7;
  size_t remaining = (size_t)strtoull(argv[5], NULL, 10);
  while (remaining > 0) {
    int wanted =
        remaining > sizeof(buffer) ? (int)sizeof(buffer) : (int)remaining;
    int count = cmu_read(&sock, buffer, wanted, NO_FLAG);
    if (count <= 0) return 8;
    if (fwrite(buffer, 1, (size_t)count, output) != (size_t)count) return 9;
    remaining -= (size_t)count;
  }
  fclose(output);
  puts("TRANSFER_DONE");
  fflush(stdout);
  if (strcmp(argv[6], "hold") == 0 && getchar() == EOF) return 10;
  if (cmu_close(&sock) < 0) {
    perror("cmu_close");
    return 11;
  }
  if (strcmp(argv[7], "check") == 0 && cmu_close(&sock) != -1) return 12;
  puts("CLOSED");
  return 0;
}
