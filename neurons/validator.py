# The MIT License (MIT)
# Copyright © 2023 Yuma Rao
# developer: Taoshi
# Copyright © 2023 Taoshi, LLC

import os
import random
import uuid
from datetime import datetime
from typing import List

import argparse
import traceback
import bittensor as bt

# Step 2: Set up the configuration parser
# This function is responsible for setting up and parsing command-line arguments.
import template
from data_generator.binance_data import BinanceData
from vali_objects.cmw.cmw_objects.cmw_client import CMWClient
from vali_objects.cmw.cmw_objects.cmw_miner import CMWMiner
from vali_objects.cmw.cmw_objects.cmw_stream_type import CMWStreamType
from vali_objects.cmw.cmw_util import CMWUtil
from vali_objects.dataclasses.base_objects.base_request_dataclass import BaseRequestDataClass
from template.protocol import TrainingForward, TrainingBackward, LiveForward, LiveBackward
from time_util.time_util import TimeUtil
from vali_objects.dataclasses.client_request import ClientRequest
from vali_objects.dataclasses.prediction_data_file import PredictionDataFile
from vali_objects.dataclasses.prediction_request import PredictionRequest
from vali_objects.dataclasses.training_request import TrainingRequest
from vali_objects.scaling.scaling import Scaling
from vali_objects.scoring.scoring import Scoring
from vali_objects.utils.vali_bkp_utils import ValiBkpUtils
from vali_objects.utils.vali_utils import ValiUtils
from vali_config import ValiConfig


def get_config():
    parser = argparse.ArgumentParser()
    # TODO(developer): Adds your custom validator arguments to the parser.
    parser.add_argument('--test_only_historical', default=0, help='if you only want to pull in '
                                                                  'historical data for testing.')
    # Adds override arguments for network and netuid.
    parser.add_argument('--netuid', type=int, default=1, help="The chain subnet uid.")
    # Adds subtensor specific arguments i.e. --subtensor.chain_endpoint ... --subtensor.network ...
    bt.subtensor.add_args(parser)
    # Adds logging specific arguments i.e. --logging.debug ..., --logging.trace .. or --logging.logging_dir ...
    bt.logging.add_args(parser)
    # Adds wallet specific arguments i.e. --wallet.name ..., --wallet.hotkey ./. or --wallet.path ...
    bt.wallet.add_args(parser)
    # Parse the config (will take command-line arguments if provided)
    # To print help message, run python3 template/miner.py --help
    config = bt.config(parser)

    # Step 3: Set up logging directory
    # Logging is crucial for monitoring and debugging purposes.
    config.full_path = os.path.expanduser(
        "{}/{}/{}/netuid{}/{}".format(
            config.logging.logging_dir,
            config.wallet.name,
            config.wallet.hotkey,
            config.netuid,
            'validator',
        )
    )
    # Ensure the logging directory exists.
    if not os.path.exists(config.full_path): os.makedirs(config.full_path, exist_ok=True)

    # Return the parsed config.
    return config


def run_time_series_validation(vali_requests: List[BaseRequestDataClass]):

    # base setup for valis
    config = get_config()
    # Set up logging with the provided configuration and directory.
    bt.logging(config=config, logging_dir=config.full_path)
    bt.logging.info(f"Running validator for subnet: {config.netuid} on network: {config.subtensor.chain_endpoint} with config:")
    # Log the configuration for reference.
    bt.logging.info(config)

    # Step 4: Build Bittensor validator objects
    # These are core Bittensor classes to interact with the network.
    bt.logging.info("Setting up bittensor objects.")

    # The wallet holds the cryptographic key pairs for the validator.
    wallet = bt.wallet( config = config )
    bt.logging.info(f"Wallet: {wallet}")

    # The subtensor is our connection to the Bittensor blockchain.
    subtensor = bt.subtensor( config = config )
    bt.logging.info(f"Subtensor: {subtensor}")

    # Dendrite is the RPC client; it lets us send messages to other nodes (axons) in the network.
    dendrite = bt.dendrite( wallet = wallet )
    bt.logging.info(f"Dendrite: {dendrite}")

    # The metagraph holds the state of the network, letting us know about other miners.
    metagraph = subtensor.metagraph( config.netuid )
    bt.logging.info(f"Metagraph: {metagraph}")

    # Step 5: Connect the validator to the network
    if wallet.hotkey.ss58_address not in metagraph.hotkeys:
        bt.logging.error(f"\nYour validator: {wallet} if not registered to chain connection: {subtensor} \nRun btcli register and try again.")
        exit()
    else:
        # Each miner gets a unique identity (UID) in the network for differentiation.
        my_subnet_uid = metagraph.hotkeys.index(wallet.hotkey.ss58_address)
        bt.logging.info(f"Running validator on uid: {my_subnet_uid}")

    # Set up initial scoring weights for validation
    # bt.logging.info("Building validation weights.")
    # scores = torch.ones_like(metagraph.S, dtype=torch.float32)
    # bt.logging.info(f"Weights: {scores}")

    for vali_request in vali_requests:
        # standardized request identifier for miners to tie together forward/backprop
        request_uuid = str(uuid.uuid4())

        if isinstance(vali_request, TrainingRequest):
            stream_type = hash(str(vali_request.stream_type) + wallet.hotkey.ss58_address)

            start_dt, end_dt, ts_ranges = ValiUtils.randomize_days(True)
            bt.logging.info(f"sending training data on stream type [{stream_type}] "
                           f"with params start date [{start_dt}] & [{end_dt}] ")

            ds = ValiUtils.get_standardized_ds()
            for ts_range in ts_ranges:
                BinanceData.get_data_and_structure_data_points(vali_request.stream_type,
                                                               ds,
                                                               ts_range)
            vmins, vmaxs, dps, sds = Scaling.scale_data_structure(ds)
            samples = bt.tensor(sds)

            training_proto = TrainingForward(
                request_uuid=request_uuid,
                stream_id=stream_type,
                samples=samples,
                topic_id=vali_request.topic_id,
                feature_ids=vali_request.feature_ids,
                schema_id=vali_request.schema_id,
                prediction_size=vali_request.prediction_size
            )

            try:
                responses = dendrite.query(
                    metagraph.axons,
                    training_proto,
                    deserialize=True
                )

                # check to see # of responses
                bt.logging.info(f"number of responses to training data: [{len(responses)}]")

                # FOR DEBUG PURPOSES
                for i, respi in enumerate(responses):
                    if respi is not None \
                            and len(respi.numpy()) == vali_request.prediction_size:
                        bt.logging.debug(f"number of responses to training data: [{len(respi.numpy())}]")
                    else:
                        bt.logging.debug(f"has no proper response")

                training_results_start = TimeUtil.timestamp_to_millis(end_dt)
                training_results_end = TimeUtil.timestamp_to_millis(end_dt) + \
                      TimeUtil.minute_in_millis(vali_request.prediction_size * ValiConfig.STANDARD_TF)

                results_ds = ValiUtils.get_standardized_ds()
                bt.logging.info("getting training results to send back to miners")

                BinanceData.get_data_and_structure_data_points(vali_request.stream_type,
                                                               results_ds,
                                                               (training_results_start, training_results_end))

                bt.logging.info("results gathered, sending back to miners")

                results_vmin, results_vmax, results_scaled = Scaling.scale_values(results_ds[0],
                                                                                      vmin=vmins[0],
                                                                                      vmax=vmaxs[0])
                results = bt.tensor(results_scaled)

                training_backprop_proto = TrainingBackward(
                    request_uuid=request_uuid,
                    stream_id=stream_type,
                    samples=results,
                    topic_id=vali_request.topic_id
                )

                dendrite.query(
                    metagraph.axons,
                    training_backprop_proto,
                    deserialize=True
                )
                bt.logging.info("results sent back to miners")

            # If we encounter an unexpected error, log it for debugging.
            except RuntimeError as e:
                bt.logging.error(e)
                traceback.print_exc()

        elif isinstance(vali_request, ClientRequest):
            stream_type = hash(str(vali_request.stream_type) + wallet.hotkey.ss58_address)

            if vali_request.client_uuid is None:
                vali_request.client_uuid = wallet.hotkey.ss58_address

            if int(config.test_only_historical) == 1:
                bt.logging.debug("using historical only with a client request")
                start_dt, end_dt, ts_ranges = ValiUtils.randomize_days(True)
            else:
                start_dt, end_dt, ts_ranges = ValiUtils.randomize_days(False)
            bt.logging.info(f"sending requested data on stream type [{stream_type}] "
                           f"with params start date [{start_dt}] & [{end_dt}] ")

            ds = ValiUtils.get_standardized_ds()
            for ts_range in ts_ranges:
                BinanceData.get_data_and_structure_data_points(vali_request.stream_type,
                                                               ds,
                                                               ts_range)
            vmins, vmaxs, dps, sds = Scaling.scale_data_structure(ds)
            samples = bt.tensor(sds)

            # forgot adding client request info to the cmw

            live_proto = LiveForward(
                request_uuid=request_uuid,
                stream_id=stream_type,
                samples=samples,
                topic_id=vali_request.topic_id,
                feature_ids=vali_request.feature_ids,
                schema_id=vali_request.schema_id,
                prediction_size=vali_request.prediction_size
            )

            try:
                vm = ValiUtils.get_vali_records()
                client = vm.get_client(vali_request.client_uuid)
                if client is None:
                    cmw_client = CMWClient().set_client_uuid(vali_request.client_uuid)
                    cmw_client.add_stream(CMWStreamType().set_stream_id(stream_type).set_topic_id(vali_request.topic_id))
                    vm.add_client(cmw_client)
                else:
                    client_stream_type = client.get_stream(stream_type)
                    if client_stream_type is None:
                        client.add_stream(CMWStreamType().set_stream_id(stream_type).set_topic_id(vali_request.topic_id))
                ValiUtils.set_vali_memory_and_bkp(CMWUtil.dump_cmw(vm))
            except Exception as e:
                # if fail to store cmw for some reason print & continue
                bt.logging.error(e)
                traceback.print_exc()

            try:
                responses = dendrite.query(
                    metagraph.axons,
                    live_proto,
                    deserialize=True
                )

                # check to see # of responses
                bt.logging.info(f"number of responses to requested data: [{len(responses)}]")

                # FOR DEBUG PURPOSES
                for i, respi in enumerate(responses):
                    if respi is not None \
                            and len(respi.numpy()) == vali_request.prediction_size:
                        bt.logging.debug(f"number of responses to requested data: [{len(respi.numpy())}]")
                    else:
                        bt.logging.debug(f"has no proper response")

                # for file name
                output_uuid = str(uuid.uuid4())

                for i, resp_i in enumerate(responses):
                    if resp_i is not None \
                            and len(resp_i.numpy()) == vali_request.prediction_size:
                        bt.logging.debug(f"axon hotkey [{metagraph.axons[i].hotkey}]")
                        # has the right number of predictions made
                        pdf = PredictionDataFile(
                            client_uuid=vali_request.client_uuid,
                            stream_type=vali_request.stream_type,
                            stream_id=stream_type,
                            topic_id=vali_request.topic_id,
                            request_uuid=request_uuid,
                            miner_uid=metagraph.axons[i].hotkey,
                            start=TimeUtil.timestamp_to_millis(end_dt),
                            end=TimeUtil.timestamp_to_millis(end_dt) + \
                                TimeUtil.minute_in_millis(vali_request.prediction_size * ValiConfig.STANDARD_TF),
                            vmins=vmins,
                            vmaxs=vmaxs,
                            decimal_places=dps,
                            predictions=resp_i.numpy()
                        )
                        ValiUtils.save_predictions_request(output_uuid, pdf)
                bt.logging.info("completed storing all predictions")

            # If we encounter an unexpected error, log it for debugging.
            except RuntimeError as e:
                bt.logging.error(e)
                traceback.print_exc()

        elif isinstance(vali_request, PredictionRequest):
            bt.logging.info("processing predictions ready to be weighed")
            # handle results ready to score and weigh
            request_df = vali_request.df
            stream_type = hash(str(request_df.stream_type) + wallet.hotkey.ss58_address)
            try:
                vm = ValiUtils.get_vali_records()

                data_structure = ValiUtils.get_standardized_ds()

                bt.logging.info("getting results from live predictions")

                BinanceData.get_data_and_structure_data_points(request_df.stream_type,
                                                               data_structure,
                                                               (request_df.start, request_df.end))

                bt.logging.info("results gathered sending back to miners via backprop and weighing")

                results_vmin, results_vmax, results_scaled = Scaling.scale_values(data_structure[0],
                                                                                      vmin=request_df.vmins[0],
                                                                                      vmax=request_df.vmaxs[0])
                # send back the results for backprop so miners can learn
                results = bt.tensor(results_scaled)

                results_backprop_proto = LiveBackward(
                    request_uuid=request_uuid,
                    stream_id=stream_type,
                    samples=results,
                    topic_id=request_df.topic_id
                )

                dendrite.query(
                    metagraph.axons,
                    results_backprop_proto,
                    deserialize=True
                )

                bt.logging.info("live results sent back to miners")

                scores = {}
                for miner_uid, miner_preds in vali_request.predictions.items():
                    scores[miner_uid] = Scoring.score_response(miner_preds, data_structure[0])

                bt.logging.debug(f"unscaled scores [{scores}]")

                scaled_scores = Scoring.simple_scale_scores(scores)
                stream_type = vm.get_client(request_df.client_uuid).get_stream(request_df.stream_id)

                # store weights for results
                sorted_scores = sorted(scaled_scores.items(), key=lambda x: x[1], reverse=True)
                winning_scores = sorted_scores[:10]

                # choose top 10
                weighed_scores = Scoring.weigh_miner_scores(winning_scores)
                weighed_winning_scores = weighed_scores[:10]
                weighed_winning_scores_dict = {score[0]: score[1] for score in weighed_winning_scores}

                bt.logging.info("scores weighed, adding to cmw")

                bt.logging.debug(f"scaled scores [{scaled_scores}]")
                bt.logging.debug(f"weighed scores [{weighed_scores}]")
                bt.logging.debug(f"weighed winning scores dict [{weighed_winning_scores_dict}]")

                try:
                    # add results to cmw
                    for miner_uid, scaled_score in scaled_scores.items():
                        bt.logging.debug(f"review mineruid [{miner_uid}]")
                        stream_miner = stream_type.get_miner(miner_uid)
                        if stream_miner is None:
                            bt.logging.debug("stream miner doesnt exist")
                            stream_miner = CMWMiner(miner_uid, 0, 0, [])
                            stream_type.add_miner(stream_miner)
                            bt.logging.debug("miner added")
                        stream_miner.add_score(scaled_score)
                        if weighed_winning_scores_dict[miner_uid] != 0:
                            bt.logging.debug(f"adding winning miner [{miner_uid}]")
                            stream_miner.add_win_value(weighed_winning_scores_dict[miner_uid])
                            stream_miner.add_win()
                    ValiUtils.set_vali_memory_and_bkp(CMWUtil.dump_cmw(vm))
                except Exception as e:
                    # if fail to store cmw for some reason print & continue
                    bt.logging.error(e)
                    traceback.print_exc()

                bt.logging.info("scores attempted to be stored in cmw, setting weights")

                weights = [item[1] for item in weighed_winning_scores]

                converted_uids = [metagraph.uids[metagraph.hotkeys.index(miner_hotkey[0])] for miner_hotkey in weighed_winning_scores]

                bt.logging.debug(f"converted uids [{converted_uids}]")
                bt.logging.debug(f"set weights [{weights}]")

                subtensor.set_weights(
                    netuid=config.netuid,  # Subnet to set weights on.
                    wallet=wallet,  # Wallet to sign set weights using hotkey.
                    uids=converted_uids,  # Uids of the miners to set weights for.
                    weights=weights  # Weights to set for the miners.
                )
                bt.logging.info("weights set and stored")
            # If we encounter an unexpected error, log it for debugging.
            except RuntimeError as e:
                bt.logging.error(e)
                traceback.print_exc()
            else:
                bt.logging.info("removing processed files")
                # remove files that have been properly processed & weighed
                for file in vali_request.files:
                    os.remove(file)


# The main function parses the configuration and runs the validator.
if __name__ == "__main__":
    while True:
        current_time = datetime.now().time()
        # if current_time.minute % 5 == 0:
        if current_time.second % 5 == 0:
            requests = []
            if current_time.second % 59 == 0:
                # see if any files exist, if not then generate a client request (a live prediction)
                all_files = ValiBkpUtils.get_all_files_in_dir(ValiBkpUtils.get_vali_predictions_dir())
                if len(all_files) == 0:
                    requests.append(ValiUtils.generate_standard_request(ClientRequest))

            # add any predictions that are ready to be scored
            requests.extend(ValiUtils.get_predictions_to_complete())

            # if no requests to fill, randomly send in a training request to help them train
            # randomize to not have all validators sending in training data requests simultaneously to assist with load
            if len(requests) == 0 and random.randint(0, 10) == 1:
                requests.append(ValiUtils.generate_standard_request(TrainingRequest))

            run_time_series_validation(requests)