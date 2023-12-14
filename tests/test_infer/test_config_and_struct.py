from colossalai.inference.config import InferenceConfig
from colossalai.inference.struct import BatchInfo, Sequence


def test_config_and_inferenceData():
    config = InferenceConfig("/llama")
    assert config.max_batch_size == 8
    sequence = Sequence(
        request_id=1,
        prompt="abc",
        input_token_id=[1, 2, 3],
        block_size=16,
        sample_params=None,
        block_table=None,
        eos_token_id=2,
        max_output_len=256,
    )

    sequence2 = Sequence(
        request_id=2,
        prompt="bcd",
        input_token_id=[4, 5, 6],
        block_size=16,
        sample_params=None,
        block_table=None,
        eos_token_id=2,
        max_output_len=256,
    )

    sequence3 = Sequence(
        request_id=3,
        prompt="efg",
        input_token_id=[7, 8, 9],
        block_size=16,
        sample_params=None,
        block_table=None,
        eos_token_id=2,
        max_output_len=256,
    )

    assert sequence.get_sentence_len() == 3
    assert sequence.get_input_len() == 3
    assert sequence.get_output_len() == 0
    assert sequence.check_finish() == False

    batch = BatchInfo.init_batch([sequence])
    batch.add_seqs([sequence2, sequence3])
    batch.add_seqs([sequence])

    assert batch.is_empty() == False
    assert batch.get_batch_size() == 3
    batch.update_batch_tokens([1, 2, 3])
    seq = batch.abort_seq(sequence)
    seq2 = batch.fliter_batch()[0]

    assert batch.get_batch_size() == 1
    assert seq.get_output_len() == 1
    assert seq.output_token_id == [1]
    assert seq2.get_output_len() == 1
    assert seq2.output_token_id == [2]

    batch.clear_batch()
    assert batch.is_empty() == True


if __name__ == "__main__":
    test_config_and_inferenceData()
