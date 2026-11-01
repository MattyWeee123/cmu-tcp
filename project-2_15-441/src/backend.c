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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

_Static_assert(MAX_LEN > sizeof(cmu_tcp_header_t) && MAX_LEN <= UINT16_MAX,
               "MAX_LEN must fit a CMU-TCP packet");
_Static_assert(MAX_NETWORK_BUFFER > 0 && MAX_NETWORK_BUFFER < INT32_MAX,
               "Buffers must fit in half the sequence space");
_Static_assert(WINDOW_INITIAL_WINDOW_SIZE > 0 && WINDOW_INITIAL_SSTHRESH > 0,
               "Initial congestion limits must be positive");
_Static_assert(DEFAULT_TIMEOUT > 0,
               "The retransmission timeout must be positive");

typedef enum { LISTEN, SYN_SENT, SYN_RECEIVED, ESTABLISHED } connection_state_t;
typedef enum {
  SLOW_START,
  CONGESTION_AVOIDANCE,
  FAST_RECOVERY
} congestion_state_t;
typedef enum {
  OPEN,
  FIN_WAIT_1,
  FIN_WAIT_2,
  CLOSE_WAIT,
  CLOSING,
  LAST_ACK,
  TIME_WAIT,
  CLOSED
} close_state_t;

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
  close_state_t close_state;
  uint32_t fin_seq;
  int64_t fin_deadline;
  int64_t linger_deadline;
  double cwnd;
  double ssthresh;
  congestion_state_t congestion_state;
  unsigned duplicate_acks;
  uint16_t peer_window;
  uint16_t last_window_sent;
  int64_t persist_deadline;
  uint8_t* reordered_data;
  uint8_t* reordered_present;
  size_t reordered_base;
  size_t reordered_count;
  int pending_fin;
  uint32_t pending_fin_seq;
  FILE* trace;
  int64_t started;
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

static uint16_t receive_window(backend_t* backend) {
  pthread_mutex_lock(&backend->sock->lock);
  /* The window is a sequence interval, including any reordered bytes already
   * inside it. Subtracting those bytes twice would shrink its right edge and
   * turn loss-generated duplicate ACKs into apparent window updates. */
  size_t available = MAX_NETWORK_BUFFER - backend->sock->receive_buffer.length;
  pthread_mutex_unlock(&backend->sock->lock);
  return available > UINT16_MAX ? UINT16_MAX : (uint16_t)available;
}

static void trace_state(backend_t* backend, const char* event) {
  if (!backend->trace) return;
  const char* states[] = {"slow_start", "congestion_avoidance",
                          "fast_recovery"};
  window_t* window = &backend->sock->window;
  fprintf(backend->trace, "%.3f,%s,%s,%.3f,%.3f,%u,%u,%u,%u\n",
          (now_ms() - backend->started) / 1000.0, event,
          states[backend->congestion_state], backend->cwnd, backend->ssthresh,
          window->next_seq_to_send - window->last_ack_received,
          backend->peer_window, window->last_ack_received,
          backend->duplicate_acks);
  fflush(backend->trace);
}

static int send_packet(backend_t* backend, uint32_t seq, uint32_t ack,
                       uint8_t flags, const uint8_t* payload, uint16_t length) {
  cmu_socket_t* sock = backend->sock;
  uint8_t packet[MAX_LEN];
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint16_t header_length = sizeof(*header);
  uint16_t packet_length = header_length + length;
  uint16_t window = receive_window(backend);
  backend->last_window_sent = window;
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
  /* Data also acknowledges the receive stream. Rebuild that ACK on each
   * retransmission because incoming data may have advanced it. */
  return send_packet(backend, segment->seq,
                     backend->sock->window.next_seq_expected, ACK_FLAG_MASK,
                     segment->payload, segment->length);
}

static int send_fin(backend_t* backend) {
  return send_packet(backend, backend->fin_seq,
                     backend->sock->window.next_seq_expected,
                     FIN_FLAG_MASK | ACK_FLAG_MASK, NULL, 0);
}

static void enter_time_wait(backend_t* backend) {
  backend->close_state = TIME_WAIT;
  /* Retain the endpoint long enough to ACK a retransmitted peer FIN. */
  backend->linger_deadline = now_ms() + 2LL * DEFAULT_TIMEOUT;
}

static void establish(backend_t* backend) {
  backend->state = ESTABLISHED;
  backend->sock->window.last_ack_received = backend->sock->initial_seq + 1;
  backend->sock->window.next_seq_to_send = backend->sock->initial_seq + 1;
}

/* ACKs are cumulative. Window updates and ACKs for FIN do not grow cwnd. */
static int acknowledge(backend_t* backend, cmu_tcp_header_t* header,
                       uint16_t payload_length) {
  cmu_socket_t* sock = backend->sock;
  window_t* window = &sock->window;
  uint32_t ack = get_ack(header);
  if (before(ack, window->last_ack_received) ||
      after(ack, window->next_seq_to_send))
    return EXIT_SUCCESS;
  uint16_t advertised = get_advertised_window(header);
  int window_changed = advertised != backend->peer_window;
  backend->peer_window = advertised;
  if (advertised) backend->persist_deadline = 0;
  if (ack == window->last_ack_received) {
    /* A duplicate ACK has no payload, no control flag, and no window change. */
    if (backend->head && !window_changed && advertised && !payload_length &&
        get_flags(header) == ACK_FLAG_MASK) {
      ++backend->duplicate_acks;
      if (backend->congestion_state == FAST_RECOVERY) {
        backend->cwnd += MSS;
        trace_state(backend, "duplicate_ack");
      } else if (backend->duplicate_acks == 3) {
        backend->ssthresh = backend->cwnd / 2;
        if (backend->ssthresh < 1) backend->ssthresh = 1;
        backend->cwnd = backend->ssthresh + 3 * MSS;
        backend->congestion_state = FAST_RECOVERY;
        trace_state(backend, "fast_retransmit");
        backend->data_deadline = now_ms() + DEFAULT_TIMEOUT;
        return send_segment(backend, backend->head);
      }
    } else if (window_changed) {
      backend->duplicate_acks = 0;
      trace_state(backend, "window_update");
    }
    return EXIT_SUCCESS;
  }

  window->last_ack_received = ack;
  if (ack == backend->fin_seq + 1) {
    if (backend->close_state == FIN_WAIT_1)
      backend->close_state = FIN_WAIT_2;
    else if (backend->close_state == CLOSING)
      enter_time_wait(backend);
    else if (backend->close_state == LAST_ACK)
      backend->close_state = CLOSED;
  }
  size_t acknowledged = 0;
  while (backend->head != NULL) {
    segment_t* segment = backend->head;
    if (before(ack, segment->seq + segment->length)) {
      if (after(ack, segment->seq)) {
        uint32_t consumed = ack - segment->seq;
        acknowledged += consumed;
        segment->length -= consumed;
        memmove(segment->payload, segment->payload + consumed, segment->length);
        segment->seq = ack;
      }
      break;
    }
    acknowledged += segment->length;
    backend->head = segment->next;
    free(segment);
  }
  if (backend->head == NULL) backend->tail = NULL;
  if (acknowledged) {
    pthread_mutex_lock(&sock->lock);
    sock->send_buffered -= acknowledged;
    pthread_cond_broadcast(&sock->wait_cond);
    pthread_mutex_unlock(&sock->lock);
    if (backend->congestion_state == FAST_RECOVERY) {
      backend->cwnd = backend->ssthresh;
      backend->congestion_state = CONGESTION_AVOIDANCE;
    } else if (backend->congestion_state == SLOW_START) {
      backend->cwnd += acknowledged < MSS ? acknowledged : MSS;
      if (backend->cwnd >= backend->ssthresh)
        backend->congestion_state = CONGESTION_AVOIDANCE;
    } else {
      /* Short segments and split ACKs must not earn a full segment's credit.
       * Otherwise filling fractional window space amplifies growth each RTT.
       */
      size_t credited = acknowledged < MSS ? acknowledged : MSS;
      backend->cwnd += (double)MSS * credited / backend->cwnd;
    }
    if (backend->cwnd > INT32_MAX) backend->cwnd = INT32_MAX;
    backend->duplicate_acks = 0;
    trace_state(backend, "new_ack");
  }
  backend->data_deadline = now_ms() + DEFAULT_TIMEOUT;
  return EXIT_SUCCESS;
}

static int fill_window(backend_t* backend) {
  cmu_socket_t* sock = backend->sock;
  window_t* window = &sock->window;
  for (;;) {
    uint32_t in_flight = window->next_seq_to_send - window->last_ack_received;
    uint32_t limit = (uint32_t)backend->cwnd;
    if (limit > backend->peer_window) limit = backend->peer_window;
    if (in_flight >= limit) return EXIT_SUCCESS;
    size_t length = limit - in_flight;
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
static int valid_packet(uint8_t* packet, ssize_t length) {
  if (length < (ssize_t)sizeof(cmu_tcp_header_t) || length > MAX_LEN) return 0;
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint16_t hlen = get_hlen(header);
  return ntohl(header->identifier) == IDENTIFIER && hlen >= sizeof(*header) &&
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
  if (!valid_packet(packet, length)) return EXIT_SUCCESS;
  cmu_tcp_header_t* header = (cmu_tcp_header_t*)packet;
  uint8_t flags = get_flags(header);
  uint32_t seq = get_seq(header);
  uint32_t ack = get_ack(header);
  uint16_t payload_length = get_payload_len(packet);

  if (backend->state == LISTEN) {
    if (flags != SYN_FLAG_MASK || payload_length != 0) return EXIT_SUCCESS;
    sock->conn = *source;
    backend->peer_initial_seq = seq;
    backend->peer_window = get_advertised_window(header);
    sock->window.next_seq_expected = seq + 1;
    backend->state = SYN_RECEIVED;
    backend->handshake_deadline = now_ms() + DEFAULT_TIMEOUT;
    return send_handshake(backend);
  }
  /* UDP performs demultiplexing. A relay can change its outer ports without
   * rewriting the embedded CMU-TCP header, so pin the actual UDP endpoint. */
  if (!same_peer(sock, source)) return EXIT_SUCCESS;

  if (backend->state == SYN_SENT) {
    if (flags != (SYN_FLAG_MASK | ACK_FLAG_MASK) || payload_length != 0 ||
        ack != sock->initial_seq + 1)
      return EXIT_SUCCESS;
    backend->peer_initial_seq = seq;
    backend->peer_window = get_advertised_window(header);
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
  if ((flags & ACK_FLAG_MASK) &&
      acknowledge(backend, header, payload_length) < 0)
    return EXIT_ERROR;
  int fin = (flags & FIN_FLAG_MASK) != 0;
  if (payload_length == 0 && !fin) return EXIT_SUCCESS;

  int receive_open = backend->close_state == OPEN ||
                     backend->close_state == FIN_WAIT_1 ||
                     backend->close_state == FIN_WAIT_2;
  if (receive_open) {
    pthread_mutex_lock(&sock->lock);
    uint32_t expected = sock->window.next_seq_expected;
    size_t span = MAX_NETWORK_BUFFER - sock->receive_buffer.length;
    if (fin && (uint32_t)(seq + payload_length - expected) <= span) {
      backend->pending_fin = 1;
      backend->pending_fin_seq = seq + payload_length;
    }
    /* A bounded byte ring handles duplicates, overlaps, and sequence wrap.
     * Its occupied bytes count against the same receive budget as unread data.
     */
    uint8_t* payload = (uint8_t*)get_payload(packet);
    for (uint32_t i = 0; i < payload_length; ++i) {
      uint32_t offset = seq + i - expected;
      if (offset >= span ||
          (backend->pending_fin && !before(seq + i, backend->pending_fin_seq)))
        continue;
      size_t index = (backend->reordered_base + offset) % MAX_NETWORK_BUFFER;
      if (!backend->reordered_present[index] &&
          sock->receive_buffer.length + backend->reordered_count <
              MAX_NETWORK_BUFFER) {
        backend->reordered_data[index] = payload[i];
        backend->reordered_present[index] = 1;
        ++backend->reordered_count;
      }
    }
    while (backend->reordered_present[backend->reordered_base] &&
           (!backend->pending_fin ||
            sock->window.next_seq_expected != backend->pending_fin_seq)) {
      size_t base = backend->reordered_base;
      size_t length = 0;
      while (
          base + length < MAX_NETWORK_BUFFER &&
          backend->reordered_present[base + length] &&
          (!backend->pending_fin ||
           sock->window.next_seq_expected + length != backend->pending_fin_seq))
        ++length;
      if (buffer_append(&sock->receive_buffer, backend->reordered_data + base,
                        length) < 0) {
        pthread_mutex_unlock(&sock->lock);
        return EXIT_ERROR;
      }
      memset(backend->reordered_present + base, 0, length);
      backend->reordered_count -= length;
      backend->reordered_base = (base + length) % MAX_NETWORK_BUFFER;
      sock->window.next_seq_expected += (uint32_t)length;
    }
    if (backend->pending_fin &&
        backend->pending_fin_seq == sock->window.next_seq_expected) {
      ++sock->window.next_seq_expected;
      sock->read_eof = 1;
      backend->pending_fin = 0;
      /* Bytes beyond the end of the stream are never delivered. */
      memset(backend->reordered_present, 0, MAX_NETWORK_BUFFER);
      backend->reordered_count = 0;
      if (backend->close_state == OPEN)
        backend->close_state = CLOSE_WAIT;
      else if (backend->close_state == FIN_WAIT_1)
        backend->close_state = CLOSING;
      else
        enter_time_wait(backend);
    }
    pthread_cond_broadcast(&sock->wait_cond);
    pthread_mutex_unlock(&sock->lock);
  }
  if (fin && backend->close_state == TIME_WAIT &&
      seq + payload_length + 1 == sock->window.next_seq_expected)
    enter_time_wait(backend);
  /* Acknowledge only the contiguous prefix, including for duplicate data. */
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
  backend.cwnd = WINDOW_INITIAL_WINDOW_SIZE;
  backend.ssthresh = WINDOW_INITIAL_SSTHRESH;
  backend.congestion_state =
      backend.cwnd < backend.ssthresh ? SLOW_START : CONGESTION_AVOIDANCE;
  backend.started = now_ms();
  backend.reordered_data = malloc(MAX_NETWORK_BUFFER);
  backend.reordered_present = calloc(MAX_NETWORK_BUFFER, 1);
  if (!backend.reordered_data || !backend.reordered_present) error = ENOMEM;
  const char* trace_path = getenv("CMU_TCP_TRACE");
  if (trace_path && *trace_path) {
    backend.trace = fopen(trace_path, "w");
    if (!backend.trace)
      error = errno;
    else {
      fputs(
          "time_s,event,state,cwnd,ssthresh,in_flight,rwnd,ack,duplicate_"
          "acks\n",
          backend.trace);
      trace_state(&backend, "initial");
    }
  }
  if (!error && backend.state == SYN_SENT) {
    backend.handshake_deadline = now_ms() + DEFAULT_TIMEOUT;
    if (send_handshake(&backend) < 0) error = errno;
  }

  while (error == 0) {
    int64_t now = now_ms();
    pthread_mutex_lock(&sock->lock);
    int drained = sock->dying && sock->send_buffer.length == 0;
    pthread_mutex_unlock(&sock->lock);
    int timeout = -1;
    /* An idle listener has no connection to tear down. Once a handshake has
     * started, finish it before sending FIN even if no data was queued. */
    if (drained && backend.state == LISTEN) break;
    if (backend.close_state == CLOSED) break;
    if (backend.close_state == TIME_WAIT) {
      if (now >= backend.linger_deadline) break;
      timeout = wait_time(backend.linger_deadline, now);
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
      if (backend.head != NULL && backend.peer_window &&
          now >= backend.data_deadline) {
        backend.ssthresh = backend.cwnd / 2;
        if (backend.ssthresh < 1) backend.ssthresh = 1;
        backend.cwnd = MSS;
        backend.congestion_state = SLOW_START;
        backend.duplicate_acks = 0;
        trace_state(&backend, "timeout");
        /* Reno restarts with one segment; the receiver retains later bytes. */
        if (send_segment(&backend, backend.head) < 0) error = errno;
        backend.data_deadline = now_ms() + DEFAULT_TIMEOUT;
      }
      if (error != 0) break;
      if (fill_window(&backend) < 0) {
        error = errno;
        break;
      }
      if (backend.head != NULL && backend.peer_window)
        timeout = wait_time(backend.data_deadline, now_ms());
      pthread_mutex_lock(&sock->lock);
      int pending = sock->send_buffered != 0;
      pthread_mutex_unlock(&sock->lock);
      if (!backend.peer_window && pending) {
        if (!backend.persist_deadline)
          backend.persist_deadline = now_ms() + DEFAULT_TIMEOUT;
        if (now_ms() >= backend.persist_deadline) {
          /* A byte before SND.UNA is already consumed (or the SYN sequence).
           * The receiver discards it and replies with its current window. */
          uint8_t probe = 0;
          if (send_packet(&backend, sock->window.last_ack_received - 1,
                          sock->window.next_seq_expected, ACK_FLAG_MASK, &probe,
                          1) < 0) {
            error = errno;
            break;
          }
          trace_state(&backend, "window_probe");
          backend.persist_deadline = now_ms() + DEFAULT_TIMEOUT;
        }
        timeout = wait_time(backend.persist_deadline, now_ms());
      }
      /* Reopen a zero window without waiting for another arriving packet.
       * Other increases can ride on the next data ACK, avoiding ACK chatter. */
      if (!backend.last_window_sent && receive_window(&backend) > 0 &&
          send_ack(&backend) < 0) {
        error = errno;
        break;
      }

      if (drained && backend.head == NULL &&
          (backend.close_state == OPEN || backend.close_state == CLOSE_WAIT)) {
        backend.fin_seq = sock->window.next_seq_to_send++;
        backend.close_state =
            backend.close_state == OPEN ? FIN_WAIT_1 : LAST_ACK;
        backend.fin_deadline = now_ms();
      }
      if (backend.close_state == FIN_WAIT_1 || backend.close_state == CLOSING ||
          backend.close_state == LAST_ACK) {
        if (now_ms() >= backend.fin_deadline) {
          if (send_fin(&backend) < 0) {
            error = errno;
            break;
          }
          backend.fin_deadline = now_ms() + DEFAULT_TIMEOUT;
        }
        timeout = wait_time(backend.fin_deadline, now_ms());
      }
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

  if (backend.trace) fclose(backend.trace);
  free(backend.reordered_data);
  free(backend.reordered_present);
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
