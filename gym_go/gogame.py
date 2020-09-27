import numpy as np
from scipy import ndimage
from sklearn import preprocessing

from gym_go import state_utils, govars

"""
The state of the game is a numpy array
* Are values are either 0 or 1

* Shape [NUM_CHNLS, SIZE, SIZE]

0 - Black pieces
1 - White pieces
2 - Turn (0 - black, 1 - white)
3 - Invalid moves (including ko-protection)
4 - Previous move was a pass
5 - Game over
"""


def init_board(size):
    # return initial board (numpy board)
    state = np.zeros((govars.NUM_CHNLS, size, size))
    return state


def next_state(state, action1d, canonical=False):
    # Deep copy the state to modify
    state = np.copy(state)

    # Initialize basic variables
    board_shape = state.shape[1:]
    pass_idx = np.prod(board_shape)
    passed = action1d == pass_idx
    action2d = action1d // board_shape[0], action1d % board_shape[1]

    player = turn(state)
    previously_passed = prev_player_passed(state)
    ko_protect = None

    if passed:
        # We passed
        state[govars.PASS_CHNL] = 1
        if previously_passed:
            # Game ended
            state[govars.DONE_CHNL] = 1
    else:
        # Move was not pass
        state[govars.PASS_CHNL] = 0

        # Assert move is valid
        assert state[govars.INVD_CHNL, action2d[0], action2d[1]] == 0, ("Invalid move", action2d)

        # Add piece
        state[player, action2d[0], action2d[1]] = 1

        # Get adjacent location and check whether the piece will be surrounded by any piece
        adj_locs, surrounded = state_utils.adj_data(state, action2d)

        # Update pieces
        killed_groups = state_utils.update_pieces(state, adj_locs, player)

        # If only killed one group, and that one group was one piece, and piece set is surrounded,
        # activate ko protection
        if len(killed_groups) == 1 and surrounded:
            killed_group = killed_groups[0]
            if len(killed_group) == 1:
                ko_protect = killed_group[0]

    # Update invalid moves
    state[govars.INVD_CHNL] = state_utils.compute_invalid_moves(state, player, ko_protect)

    # Switch turn
    state_utils.set_turn(state)

    if canonical:
        # Set canonical form
        state = canonical_form(state)

    return state


def batch_next_states(batch_states, batch_action1d, canonical=False):
    # Deep copy the state to modify
    batch_states = np.copy(batch_states)

    # Initialize basic variables
    board_shape = batch_states.shape[2:]
    pass_idx = np.prod(board_shape)
    batch_pass = np.nonzero(batch_action1d == pass_idx)
    batch_non_pass = np.nonzero(batch_action1d != pass_idx)[0]
    batch_prev_passed = batch_prev_player_passed(batch_states)
    batch_game_ended = np.nonzero(batch_prev_passed & (batch_action1d == pass_idx))
    batch_action2d = np.array([batch_action1d[batch_non_pass] // board_shape[0],
                               batch_action1d[batch_non_pass] % board_shape[1]]).T

    batch_players = batch_turn(batch_states)
    batch_non_pass_players = batch_players[batch_non_pass]
    batch_ko_protect = np.empty(len(batch_states), dtype=object)

    # Pass moves
    batch_states[batch_pass, govars.PASS_CHNL] = 1
    # Game ended
    batch_states[batch_game_ended, govars.DONE_CHNL] = 1

    # Non-pass moves
    batch_states[batch_non_pass, govars.PASS_CHNL] = 0

    # Assert all non-pass moves are valid
    assert (batch_states[batch_non_pass, govars.INVD_CHNL, batch_action2d[:, 0], batch_action2d[:, 1]] == 0).all()

    # Add piece
    batch_states[batch_non_pass, batch_non_pass_players, batch_action2d[:, 0], batch_action2d[:, 1]] = 1

    # Get adjacent location and check whether the piece will be surrounded by any piece
    batch_adj_locs, batch_surrounded = state_utils.batch_adj_data(batch_states[batch_non_pass], batch_action2d)

    # Update pieces
    batch_killed_groups = state_utils.batch_update_pieces(batch_non_pass, batch_states, batch_adj_locs,
                                                          batch_non_pass_players)

    # Ko-protection
    for i, (killed_groups, surrounded) in enumerate(zip(batch_killed_groups, batch_surrounded)):
        # If only killed one group, and that one group was one piece, and piece set is surrounded,
        # activate ko protection
        if len(killed_groups) == 1 and surrounded:
            killed_group = killed_groups[0]
            if len(killed_group) == 1:
                batch_ko_protect[batch_non_pass[i]] = killed_group[0]

    # Update invalid moves
    batch_states[:, govars.INVD_CHNL] = state_utils.batch_compute_invalid_moves(batch_states, batch_players,
                                                                                batch_ko_protect)

    # Switch turn
    state_utils.batch_set_turn(batch_states)

    if canonical:
        # Set canonical form
        batch_states = batch_canonical_form(batch_states)

    return batch_states


def valid_moves(state):
    # return a fixed size binary vector
    if game_ended(state):
        return np.zeros(action_size(state))
    return np.append(1 - state[govars.INVD_CHNL].flatten(), 1)


def children(state, canonical=False, padded=True):
    valid_moves_bool = valid_moves(state)
    n = len(valid_moves_bool)
    valid_move_idcs = np.argwhere(valid_moves_bool).flatten()
    batch_states = np.tile(state[np.newaxis], (len(valid_move_idcs), 1, 1, 1))
    children = batch_next_states(batch_states, valid_move_idcs, canonical)

    if padded:
        padded_children = np.zeros((n, *state.shape))
        padded_children[valid_move_idcs] = children
        children = padded_children
    return children


def invalid_moves(state):
    # return a fixed size binary vector
    if game_ended(state):
        return np.zeros(action_size(state))
    return np.append(state[govars.INVD_CHNL].flatten(), 0)


def action_size(state=None, board_size: int = None):
    # return number of actions
    if state is not None:
        m, n = state.shape[1:]
    elif board_size is not None:
        m, n = board_size, board_size
    else:
        raise RuntimeError('No argument passed')
    return m * n + 1


def prev_player_passed(state):
    return np.max(state[govars.PASS_CHNL] == 1) == 1


def batch_prev_player_passed(batch_states):
    return np.max(batch_states[:, govars.PASS_CHNL], axis=(1, 2)) == 1


def game_ended(state):
    """
    :param state:
    :return: 0/1 = game not ended / game ended respectively
    """
    m, n = state.shape[1:]
    return int(np.count_nonzero(state[govars.DONE_CHNL] == 1) == m * n)


def winning(state, komi=0):
    black_area, white_area = areas(state)
    area_difference = black_area - white_area
    komi_correction = area_difference - komi

    if komi_correction > 0:
        return 1
    elif komi_correction == 0:
        return 0
    else:
        assert komi_correction < 0
        return -1


def turn(state):
    """
    :param state:
    :return: Who's turn it is (govars.BLACK/govars.WHITE)
    """
    return int(np.max(state[govars.TURN_CHNL]))


def batch_turn(batch_state):
    return np.max(batch_state[:, govars.TURN_CHNL], axis=(1, 2)).astype(np.int)


def liberties(state: np.ndarray):
    blacks = state[govars.BLACK]
    whites = state[govars.WHITE]
    all_pieces = np.sum(state[[govars.BLACK, govars.WHITE]], axis=0)

    liberty_list = []
    for player_pieces in [blacks, whites]:
        liberties = ndimage.binary_dilation(player_pieces, state_utils.surround_struct)
        liberties *= (1 - all_pieces).astype(np.bool)
        liberty_list.append(liberties)

    return liberty_list[0], liberty_list[1]


def num_liberties(state: np.ndarray):
    black_liberties, white_liberties = liberties(state)
    black_liberties = np.count_nonzero(black_liberties)
    white_liberties = np.count_nonzero(white_liberties)

    return black_liberties, white_liberties


def areas(state):
    '''
    Return black area, white area
    '''

    all_pieces = np.sum(state[[govars.BLACK, govars.WHITE]], axis=0)
    empties = 1 - all_pieces

    empty_labels, num_empty_areas = ndimage.measurements.label(empties)

    black_area, white_area = np.sum(state[govars.BLACK]), np.sum(state[govars.WHITE])
    for label in range(1, num_empty_areas + 1):
        empty_area = empty_labels == label
        neighbors = ndimage.binary_dilation(empty_area)
        black_claim = False
        white_claim = False
        if (state[govars.BLACK] * neighbors > 0).any():
            black_claim = True
        if (state[govars.WHITE] * neighbors > 0).any():
            white_claim = True
        if black_claim and not white_claim:
            black_area += np.sum(empty_area)
        elif white_claim and not black_claim:
            white_area += np.sum(empty_area)

    return black_area, white_area


def canonical_form(state):
    state = np.copy(state)
    if turn(state) == govars.WHITE:
        num_channels = state.shape[0]
        channels = np.arange(num_channels)
        channels[govars.BLACK] = govars.WHITE
        channels[govars.WHITE] = govars.BLACK
        state = state[channels]
        state_utils.set_turn(state)
    return state


def batch_canonical_form(batch_state):
    batch_state = np.copy(batch_state)
    batch_player = batch_turn(batch_state)
    white_players_idcs = np.nonzero(batch_player == govars.WHITE)[0]

    num_channels = batch_state.shape[1]
    channels = np.arange(num_channels)
    channels[govars.BLACK] = govars.WHITE
    channels[govars.WHITE] = govars.BLACK

    for i in white_players_idcs:
        batch_state[i] = batch_state[i, channels]

    state_utils.batch_set_turn(batch_state)
    return batch_state


def random_symmetry(image):
    """
    Returns a random symmetry of the image
    :param image: A (C, BOARD_SIZE, BOARD_SIZE) numpy array, where C is any number
    :return:
    """
    orientation = np.random.randint(0, 8)

    if (orientation >> 0) % 2:
        # Horizontal flip
        image = np.flip(image, 2)
    if (orientation >> 1) % 2:
        # Vertical flip
        image = np.flip(image, 1)
    if (orientation >> 2) % 2:
        # Rotate 90 degrees
        image = np.rot90(image, axes=(1, 2))

    return image


def all_symmetries(image):
    """
    :param image: A (C, BOARD_SIZE, BOARD_SIZE) numpy array, where C is any number
    :return: All 8 orientations that are symmetrical in a Go game over the 2nd and 3rd axes
    (i.e. rotations, flipping and combos of them)
    """
    symmetries = []

    for i in range(8):
        x = image
        if (i >> 0) % 2:
            # Horizontal flip
            x = np.flip(x, 2)
        if (i >> 1) % 2:
            # Vertical flip
            x = np.flip(x, 1)
        if (i >> 2) % 2:
            # Rotation 90 degrees
            x = np.rot90(x, axes=(1, 2))
        symmetries.append(x)

    return symmetries


def random_weighted_action(move_weights):
    """
    Assumes all invalid moves have weight 0
    Action is 1D
    Expected shape is (NUM OF MOVES, )
    """
    move_weights = preprocessing.normalize(move_weights[np.newaxis], norm='l1')
    return np.random.choice(np.arange(len(move_weights[0])), p=move_weights[0])


def random_action(state):
    """
    Assumed to be (NUM_CHNLS, BOARD_SIZE, BOARD_SIZE)
    Action is 1D
    """
    invalid_moves = state[govars.INVD_CHNL].flatten()
    invalid_moves = np.append(invalid_moves, 0)
    move_weights = 1 - invalid_moves

    return random_weighted_action(move_weights)


def str(state):
    board_str = ' '

    size = state.shape[1]
    for i in range(size):
        board_str += '   {}'.format(i)
    board_str += '\n  '
    board_str += '----' * size + '-'
    board_str += '\n'
    for i in range(size):
        board_str += '{} |'.format(i)
        for j in range(size):
            if state[0, i, j] == 1:
                board_str += ' B'
            elif state[1, i, j] == 1:
                board_str += ' W'
            elif state[2, i, j] == 1:
                board_str += ' .'
            else:
                board_str += '  '

            board_str += ' |'

        board_str += '\n  '
        board_str += '----' * size + '-'
        board_str += '\n'

    black_area, white_area = areas(state)
    game_ended = game_ended(state)
    prev_player_passed = prev_player_passed(state)
    turn = turn(state)
    board_str += '\tTurn: {}, Last Turn Passed: {}, Game Over: {}\n'.format('B' if turn == 0 else 'W',
                                                                            prev_player_passed,
                                                                            game_ended)
    board_str += '\tBlack Area: {}, White Area: {}\n'.format(black_area, white_area)
    return board_str
