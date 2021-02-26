import argparse
import csv
import logging
import numpy as np
import os
import pandas as pd
import sys

import pyarrow as pa
import pyarrow.parquet as pq

from namematch.base import NamematchBase
from namematch.data_structures.schema import Schema
from namematch.data_structures.parameters import Parameters
from namematch.utils.utils import equip_logger_id, log_runtime_and_memory, load_yaml, setup_logging

try:
    profile
except:
    from line_profiler import LineProfiler
    profile = LineProfiler()

class GenerateMustLinks(NamematchBase):
    def __init__(
        self,
        params,
        schema,
        all_names_file,
        output_file='output_temp/must_links.csv',
        logger_id=None,
        *args,
        **kwargs
    ):

        super(GenerateMustLinks, self).__init__(params, schema, output_file, logger_id, *args, **kwargs)

        self.all_names_file = all_names_file

    @equip_logger_id
    @log_runtime_and_memory
    def main__generate_must_links(self, **kw):
        '''Generate the list of must-link pairs using UniqueID and ExistingID info .

        Args:
            params (Parameters object): contains parameter values
            schema (Schema object): contains match schema info (files to match, variables to use, etc.)
            all_names_file (str): path to output_temp's all-names file
            output_file (str): path to output_temp's must-links file
        '''

        global logger

        logger_id = kw.get('logger_id')
        if logger_id:
            logger = logging.getLogger(f'namematch_{str(logger_id)}')

        else:
            logger = self.logger

        logger.info('Generating "must-link" record pairs.')

        # get UniqueID variables
        uid_vars_list = self.schema.variables.get_variables_where(
                attr='compare_type', attr_value='UniqueID')

        # get ExistingID variables (incremental)
        eid_vars_list = self.schema.variables.get_variables_where(
                attr='compare_type', attr_value='ExistingID')

        # get records with non-missing unique identifiers
        ml_var_df = self.build_ml_var_df(
                self.all_names_file, uid_vars_list, eid_vars_list)

        # get the "must-link" record pairs
        must_links_df = self.get_must_links(
                ml_var_df, uid_vars_list, eid_vars_list)

        # true record pairs
        must_links_df.to_csv(self.output_file, index=False,
                quoting=csv.QUOTE_NONNUMERIC)

    def build_ml_var_df(self, all_names_file, uid_vars_list, eid_vars_list, **kw):
        '''Load the all-names file and limit it to the rows that have either 
        a non-missing UniqueID or ExistingID value. 
        
        Args: 
            all_names_file (str): path to output_temp's all-names file
            uid_vars_list (list of strings): all-name columns with compare_type "UniqueID" 
            eid_vars_list (list of strings): all-name columns with compare_type "ExistingID"

        Returns: 
            pd.DataFrame: a subset of the all-names file, relevant colums only
                ======================   =======================================================
                record_id                unique record identifier 
                blockstring              concatenation of all the blocking variables (sep by ::) 
                file_type                either "new" or "existing"
                drop_from_nm             flag, 1 if met any "to drop" criteria 0 otherwise 
                <UniqueID column(s)>     variables of compare_type UniqueID
                <ExistingID column(s)>   variables of compare_type ExistingID
                has_ml_var               flag, always 1 in output (ml stands for must-link)
                ======================   =======================================================    
        '''

        cols = ['record_id', 'blockstring', 'drop_from_nm',
                'file_type'] + uid_vars_list + eid_vars_list

        table = pq.read_table(all_names_file)
        an = table.to_pandas()[cols]

        an['has_ml_var'] = 0
        for ml_var in (uid_vars_list + eid_vars_list):
            an.loc[an[ml_var] != '', 'has_ml_var'] = 1

        return an[an.has_ml_var == 1]

    @profile
    @equip_logger_id
    @log_runtime_and_memory
    def get_must_links(self, ml_var_df, uid_vars_list, eid_vars_list, **kw):
        '''Expand the list of records with must-link information to pairs of records
        that must be linked togehter in the final match. 

        Args: 
            ml_var_df (pd.DataFrame): 
                ======================   =======================================================
                record_id                unique record identifier 
                blockstring              concatenation of all the blocking variables (sep by ::) 
                file_type                either "new" or "existing"
                drop_from_nm             flag, 1 if met any "to drop" criteria 0 otherwise 
                <UniqueID column(s)>     variables of compare_type UniqueID
                <ExistingID column(s)>   variables of compare_type ExistingID
                has_ml_var               flag, always 1 in output (ml stands for must-link)
                ======================   =======================================================    

            uid_vars_list (list of strings): all-name columns with compare_type "UniqueID" 
            eid_vars_list (list of strings): all-name columns with compare_type "ExistingID"

        Returns: 
            pd.DataFrame: list of must-link record pairs
                ===================   =======================================================
                record_id_1           unique identifier for the first record in the pair
                record_id_2           unique identifier for the second record in the pair
                blockstring_1         blockstring for the first record in the pair
                blockstring_2         blockstring for the second record in the pair
                drop_from_nm_1        flag, 1 if the first record in the pair was not eligible for matching
                drop_from_nm_2        flag, 1 if the second record in the pair was not eligible for matching
                existing              flag, 1 if the pair is must-link because of ExistingID
                ===================   =======================================================  
        '''

        ml_var_df = ml_var_df.copy()

        # for each UniqueID variable, merge the data frame on itself to get must-links
        must_link_df_list = []
        for ml_var in (uid_vars_list + eid_vars_list):

            # warn if any uids are used more than n times (might be
            # sign of misspecified missing uid)
            if ml_var in uid_vars_list:
                uid_counts = ml_var_df[ml_var_df[ml_var] != ''].groupby(ml_var).size()
                uid_counts_high = uid_counts[uid_counts > 200].index.tolist()
                if len(uid_counts_high) > 0:
                    logger.warning(f"The following {ml_var} values have over 200 unique "
                                   f"values; please ensure strings such as 'NA' are not "
                                   f"getting coded as values.")
                    logger.info(uid_counts_high)
                if uid_counts.max() > 1000:
                    raise()

            must_link_df = pd.merge(
                    ml_var_df[ml_var_df[ml_var] != ''], ml_var_df,
                    on=ml_var, suffixes=['_1', '_2'])

            must_link_df = must_link_df[
                    (must_link_df.blockstring_1 < must_link_df.blockstring_2) |
                    ((must_link_df.blockstring_1 == must_link_df.blockstring_2) &
                     (must_link_df.record_id_1 < must_link_df.record_id_2))]

            must_link_df['existing'] = 0

            # tweak for incremental matching
            if len(eid_vars_list) > 0:
                if ml_var in uid_vars_list:
                    must_link_df = must_link_df[
                            (must_link_df.file_type_1 == 'new') |
                            (must_link_df.file_type_2 == 'new')]
                else:
                    must_link_df['existing'] = 1

            must_link_df = must_link_df[
                    ['record_id_1', 'record_id_2', 'blockstring_1', 'blockstring_2',
                    'drop_from_nm_1', 'drop_from_nm_2', 'existing']]

            must_link_df_list.append(must_link_df.copy())

        must_link_df = pd.concat(must_link_df_list)
        must_link_df = must_link_df.sort_values(['existing'], ascending=[False])
        # NOTE: existing == 1 takes priority over existing == 0

        # remove duplicates (would occur if records match on multiple Unique IDs)
        len_pre_drop_duplicates = len(must_link_df)
        must_link_df = must_link_df.drop_duplicates(subset=['record_id_1', 'record_id_2'])
        len_post_drop_duplicates = len(must_link_df)
        if (len_pre_drop_duplicates > len_post_drop_duplicates):
            logger.debug(f'Dropped {len_pre_drop_duplicates - len_post_drop_duplicates} '
                         f'duplicate ground truth rows')

        return must_link_df


if __name__ == '__main__':

    parser = argparse.ArgumentParser()
    parser.add_argument('--params_file')
    parser.add_argument('--schema_file')
    parser.add_argument('--all_names_file')
    parser.add_argument('--output_file')
    parser.add_argument('--log_file')
    parser.add_argument('--nm_code_dir')
    args = parser.parse_args()

    params = Parameters.load(args.params_file)
    schema = Schema.load(args.schema_file)

    logging_params = load_yaml(os.path.join(args.nm_code_dir, 'utils/logging_config.yaml'))

    generate_must_links = GenerateMustLinks(
        params=params,
        schema=schema,
        output_file=args.output_file,
        all_names_file=args.all_names_file,
    )
    generate_must_links.logger_init(logging_params, args.log_file)
    generate_must_links.main__generate_must_links()