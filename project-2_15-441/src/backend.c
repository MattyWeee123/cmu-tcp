/**
 * Copyright (C) 2022 Carnegie Mellon University
 *
 * This file is part of the TCP in the Wild course project developed for the
 * Computer Networks course (15-441/641) taught at Carnegie Mellon University.
 *
 * No part of the project may be copied and/or distributed without the express
 * permission of the 15-441/641 course staff.
 *
 *
 * This file implements the CMU-TCP backend. The backend runs in a different
 * thread and handles all the socket operations separately from the application.
 *
 * This is where most of your code should go. Feel free to modify any function
 * in this file.
 */

#include "backend.h"

#include <arpa/inet.h>
#include <errno.h>
#include <limits.h>
#include <poll.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

_Static_assert(MAX_LEN > sizeof(cmu_tcp_header_t) && MAX_LEN <= UINT16_MAX,
               "MAX_LEN must fit a CMU-TCP packet");
_Static_assert(CP1_WINDOW_SIZE > 0 && CP1_WINDOW_SIZE < INT32_MAX,
               "The sending window must fit in half the sequence space");
_Static_assert(DEFAULT_TIMEOUT > 0,
               "The retransmission timeout must be positive");

typedef enum { LISTEN, SYN_SENT, SYN_RECEIVED, ESTABLISHED } connection_state_t;

typedef struct segment {
  struct segment* next;
  uint32_t seq;
  uint16_t length;
  uint8_t payload[];
} segment_t;

/* Only the backend thread touches this state and the socket's sequence numbers.
 */
typedef struct {
  cmu_socket_t* sock;
  connection_state_t state;
  uint32_t peer_initial_seq;
  segment_t* head;
  segment_t* tail;
  int64_t handshake_deadline;
  int64_t data_deadline;
  int64_t last_data_received;
  int received_data;
} backend_t;

static int64_t now_ms(void) {
  struct timespec now;
  clock_gettime(CLOCK_MONOTONIC, &now);
  return (int64_t)now.tv_sec * 1000 + now.tv_nsec / 1000000;
}

static int retryable_network_error(int error) {
  return error == EAGAIN || error == EWOULDBLOCK || error == ENOBUFS ||
         error == ECONNREFUSED || error == EHOSTUNREACH || error == ENETUNREACH;
}

static int send_packet(backend_t* backend, uint32_t seq, uint32_t ack,
                       uint8_t flags, const uint8_t* payload, uint16_t length) {
  cmu_socket_t* sock = backend->sock;
  uint8_t packet[MAX_LEN];
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint16_t header_length = sizeof(*header);
  uint16_t packet_length = header_length + length;
  uint16_t window = CP1_WINDOW_SIZE > UINT16_MAX ? UINT16_MAX : CP1_WINDOW_SIZE;
  /* A non-NULL extension pointer also keeps the provided zero-length memcpy
   * in set_header well-defined when no extensions are present. */
  set_header(header, sock->my_port, ntohs(sock->conn.sin_port), seq, ack,
             header_length, packet_length, flags, window, 0, packet);
  if (length > 0) memcpy(packet + header_length, payload, length);
  ssize_t result;
  do {
    result = sendto(sock->socket, packet, packet_length, 0,
                    (struct sockaddr*)&sock->conn, sizeof(sock->conn));
  } while (result < 0 && errno == EINTR);
  /* Queue pressure and transient ICMP errors are equivalent to packet loss.
   * In particular, a listener may not have started when the first SYN is sent.
   */
  if (result < 0 && retryable_network_error(errno)) return EXIT_SUCCESS;
  return result < 0 ? EXIT_ERROR : EXIT_SUCCESS;
}

static int send_ack(backend_t* backend) {
  window_t* window = &backend->sock->window;
  return send_packet(backend, window->next_seq_to_send,
                     window->next_seq_expected, ACK_FLAG_MASK, NULL, 0);
}

static int send_handshake(backend_t* backend) {
  cmu_socket_t* sock = backend->sock;
  uint8_t flags = SYN_FLAG_MASK;
  uint32_t ack = 0;
  if (sock->type == TCP_LISTENER) {
    flags |= ACK_FLAG_MASK;
    ack = backend->peer_initial_seq + 1;
  }
  return send_packet(backend, sock->initial_seq, ack, flags, NULL, 0);
}

static int send_final_ack(backend_t* backend) {
  return send_packet(backend, backend->sock->initial_seq + 1,
                     backend->peer_initial_seq + 1, ACK_FLAG_MASK, NULL, 0);
}

static int send_segment(backend_t* backend, segment_t* segment) {
  return send_packet(backend, segment->seq,
                     backend->sock->window.next_seq_expected, ACK_FLAG_MASK,
                     segment->payload, segment->length);
}

static void establish(backend_t* backend) {
  backend->state = ESTABLISHED;
  backend->sock->window.last_ack_received = backend->sock->initial_seq + 1;
  backend->sock->window.next_seq_to_send = backend->sock->initial_seq + 1;
}

/* ACKs are cumulative, but must never acknowledge bytes we have not sent. */
static void acknowledge(backend_t* backend, uint32_t ack) {
  window_t* window = &backend->sock->window;
  if (!after(ack, window->last_ack_received) ||
      after(ack, window->next_seq_to_send))
    return;

  window->last_ack_received = ack;
  while (backend->head != NULL) {
    segment_t* segment = backend->head;
    if (before(ack, segment->seq + segment->length)) {
      if (after(ack, segment->seq)) {
        uint32_t consumed = ack - segment->seq;
        segment->length -= consumed;
        memmove(segment->payload, segment->payload + consumed, segment->length);
        segment->seq = ack;
      }
      break;
    }
    backend->head = segment->next;
    free(segment);
  }
  if (backend->head == NULL) backend->tail = NULL;
  backend->data_deadline = now_ms() + DEFAULT_TIMEOUT;
}

static int fill_window(backend_t* backend) {
  cmu_socket_t* sock = backend->sock;
  window_t* window = &sock->window;
  for (;;) {
    uint32_t in_flight = window->next_seq_to_send - window->last_ack_received;
    if (in_flight >= CP1_WINDOW_SIZE) return EXIT_SUCCESS;
    size_t length = CP1_WINDOW_SIZE - in_flight;
    if (length > MSS) length = MSS;

    pthread_mutex_lock(&sock->lock);
    if (length > sock->send_buffer.length) length = sock->send_buffer.length;
    if (length == 0) {
      pthread_mutex_unlock(&sock->lock);
      return EXIT_SUCCESS;
    }
    segment_t* segment = malloc(sizeof(*segment) + length);
    if (segment == NULL) {
      pthread_mutex_unlock(&sock->lock);
      return EXIT_ERROR;
    }
    segment->next = NULL;
    segment->seq = window->next_seq_to_send;
    segment->length = (uint16_t)length;
    memcpy(segment->payload, sock->send_buffer.data + sock->send_buffer.offset,
           length);
    buffer_consume(&sock->send_buffer, length);
    pthread_mutex_unlock(&sock->lock);

    if (send_segment(backend, segment) < 0) {
      free(segment);
      return EXIT_ERROR;
    }
    if (backend->tail != NULL) {
      backend->tail->next = segment;
    } else {
      backend->head = segment;
      backend->data_deadline = now_ms() + DEFAULT_TIMEOUT;
    }
    backend->tail = segment;
    window->next_seq_to_send += (uint32_t)length;
  }
}

/* Validate one complete UDP datagram before trusting lengths or peer identity.
 */
static int valid_packet(cmu_socket_t* sock, uint8_t* packet, ssize_t length,
                        const struct sockaddr_in* source) {
  if (length < (ssize_t)sizeof(cmu_tcp_header_t) || length > MAX_LEN) return 0;
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint16_t hlen = get_hlen(header);
  return ntohl(header->identifier) == IDENTIFIER &&
         get_src(header) == ntohs(source->sin_port) &&
         get_dst(header) == sock->my_port && hlen >= sizeof(*header) &&
         hlen <= length &&
         hlen == sizeof(*header) + get_extension_length(header) &&
         get_plen(header) == length &&
         !(get_flags(header) &
           ~(SYN_FLAG_MASK | ACK_FLAG_MASK | FIN_FLAG_MASK));
}

static int same_peer(cmu_socket_t* sock, const struct sockaddr_in* source) {
  return source->sin_addr.s_addr == sock->conn.sin_addr.s_addr &&
         source->sin_port == sock->conn.sin_port;
}

static int handle_packet(backend_t* backend, uint8_t* packet, ssize_t length,
                         const struct sockaddr_in* source) {
  cmu_socket_t* sock = backend->sock;
  if (!valid_packet(sock, packet, length, source)) return EXIT_SUCCESS;
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint8_t flags = get_flags(header);
  uint32_t seq = get_seq(header);
  uint32_t ack = get_ack(header);
  uint16_t payload_length = get_payload_len(packet);

  if (backend->state == LISTEN) {
    if (flags != SYN_FLAG_MASK || payload_length != 0) return EXIT_SUCCESS;
    sock->conn = *source;
    backend->peer_initial_seq = seq;
    sock->window.next_seq_expected = seq + 1;
    backend->state = SYN_RECEIVED;
    backend->handshake_deadline = now_ms() + DEFAULT_TIMEOUT;
    return send_handshake(backend);
  }
  if (!same_peer(sock, source)) return EXIT_SUCCESS;

  if (backend->state == SYN_SENT) {
    if (flags != (SYN_FLAG_MASK | ACK_FLAG_MASK) || payload_length != 0 ||
        ack != sock->initial_seq + 1)
      return EXIT_SUCCESS;
    backend->peer_initial_seq = seq;
    sock->window.next_seq_expected = seq + 1;
    establish(backend);
    return send_final_ack(backend);
  }

  if (backend->state == SYN_RECEIVED) {
    if (flags == SYN_FLAG_MASK && payload_length == 0 &&
        seq == backend->peer_initial_seq)
      return send_handshake(backend);
    if (flags != ACK_FLAG_MASK || ack != sock->initial_seq + 1 ||
        seq != sock->window.next_seq_expected)
      return EXIT_SUCCESS;
    establish(backend);
    /* The last handshake ACK may carry data, including when a pure ACK was
     * lost. */
  }

  if (flags & SYN_FLAG_MASK) {
    if (payload_length != 0 || seq != backend->peer_initial_seq)
      return EXIT_SUCCESS;
    if (sock->type == TCP_INITIATOR &&
        flags == (SYN_FLAG_MASK | ACK_FLAG_MASK) &&
        ack == sock->initial_seq + 1)
      return send_final_ack(backend);
    if (sock->type == TCP_LISTENER && flags == SYN_FLAG_MASK)
      return send_handshake(backend);
    return EXIT_SUCCESS;
  }
  /* FIN teardown is the optional extension, not part of CP1 data transfer. */
  if (flags != 0 && flags != ACK_FLAG_MASK) return EXIT_SUCCESS;
  if (flags & ACK_FLAG_MASK) acknowledge(backend, ack);
  if (payload_length == 0) return EXIT_SUCCESS;

  backend->received_data = 1;
  backend->last_data_received = now_ms();
  if (seq == sock->window.next_seq_expected) {
    pthread_mutex_lock(&sock->lock);
    int result = buffer_append(&sock->receive_buffer, get_payload(packet),
                               payload_length);
    if (result == EXIT_SUCCESS) {
      sock->window.next_seq_expected += payload_length;
      pthread_cond_broadcast(&sock->wait_cond);
    }
    pthread_mutex_unlock(&sock->lock);
    if (result < 0) return EXIT_ERROR;
  }
  /* Go-Back-N discards out-of-order and duplicate payloads, acknowledging only
   * the contiguous prefix. Even duplicates need ACKs to recover a lost ACK. */
  return send_ack(backend);
}

static int receive_packets(backend_t* backend) {
  /* Bound each batch so a busy peer cannot starve timers or application writes.
   */
  for (int i = 0; i < 64; ++i) {
    uint8_t packet[MAX_LEN + 1];
    struct sockaddr_in source;
    socklen_t source_length = sizeof(source);
    ssize_t length = recvfrom(backend->sock->socket, packet, sizeof(packet), 0,
                              (struct sockaddr*)&source, &source_length);
    if (length < 0) {
      if (errno == EINTR) continue;
      if (retryable_network_error(errno)) return EXIT_SUCCESS;
      return EXIT_ERROR;
    }
    if (handle_packet(backend, packet, length, &source) < 0) return EXIT_ERROR;
  }
  return EXIT_SUCCESS;
}

static int wait_time(int64_t deadline, int64_t now) {
  if (deadline <= now) return 0;
  int64_t remaining = deadline - now;
  return remaining > INT_MAX ? INT_MAX : (int)remaining;
}

void* begin_backend(void* in) {
  cmu_socket_t* sock = in;
  backend_t backend = {0};
  backend.sock = sock;
  backend.state = sock->type == TCP_LISTENER ? LISTEN : SYN_SENT;
  int error = 0;
  if (backend.state == SYN_SENT) {
    backend.handshake_deadline = now_ms() + DEFAULT_TIMEOUT;
    if (send_handshake(&backend) < 0) error = errno;
  }

  while (error == 0) {
    int64_t now = now_ms();
    pthread_mutex_lock(&sock->lock);
    int drained = sock->dying && sock->send_buffer.length == 0;
    pthread_mutex_unlock(&sock->lock);
    int timeout = -1;
    if (drained && backend.head == NULL) {
      /* There is no FIN exchange in the mandatory checkpoint. Briefly retain
       * the receiver so a peer can recover if the final data ACK was lost. */
      if (!backend.received_data ||
          now >= backend.last_data_received + 2LL * DEFAULT_TIMEOUT)
        break;
      timeout =
          wait_time(backend.last_data_received + 2LL * DEFAULT_TIMEOUT, now);
    }

    if (backend.state == SYN_SENT || backend.state == SYN_RECEIVED) {
      if (now >= backend.handshake_deadline) {
        if (send_handshake(&backend) < 0) {
          error = errno;
          break;
        }
        backend.handshake_deadline = now_ms() + DEFAULT_TIMEOUT;
      }
      timeout = wait_time(backend.handshake_deadline, now_ms());
    } else if (backend.state == ESTABLISHED) {
      if (backend.head != NULL && now >= backend.data_deadline) {
        /* Retransmit every outstanding segment, in order, on one oldest-byte
         * timer. Duplicate/stale ACKs never restart this timer. */
        for (segment_t* segment = backend.head; segment;
             segment = segment->next) {
          if (send_segment(&backend, segment) < 0) {
            error = errno;
            break;
          }
        }
        backend.data_deadline = now_ms() + DEFAULT_TIMEOUT;
      }
      if (error != 0) break;
      if (fill_window(&backend) < 0) {
        error = errno;
        break;
      }
      if (backend.head != NULL)
        timeout = wait_time(backend.data_deadline, now_ms());
    }

    struct pollfd fds[2] = {{sock->socket, POLLIN, 0},
                            {sock->wake_pipe[0], POLLIN, 0}};
    int ready = poll(fds, 2, timeout);
    if (ready < 0) {
      if (errno == EINTR) continue;
      error = errno;
      break;
    }
    if (fds[0].revents & POLLNVAL) {
      error = EBADF;
      break;
    }
    if (fds[1].revents & POLLIN) {
      uint8_t wakeups[128];
      while (read(sock->wake_pipe[0], wakeups, sizeof(wakeups)) > 0) {
      }
    }
    if ((fds[0].revents & (POLLIN | POLLERR)) && receive_packets(&backend) < 0)
      error = errno;
  }

  while (backend.head != NULL) {
    segment_t* next = backend.head->next;
    free(backend.head);
    backend.head = next;
  }
  pthread_mutex_lock(&sock->lock);
  sock->error = error;
  sock->backend_done = 1;
  pthread_cond_broadcast(&sock->wait_cond);
  pthread_mutex_unlock(&sock->lock);
  return NULL;
}
