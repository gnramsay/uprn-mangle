"""Django command to format and import UPRN data to our database."""
import os
from glob import glob
from pathlib import Path
from shutil import copyfile

import pandas as pd

# load constants from external file so we can share it
from api.management.support.constants import (
    CROSSREF_DIR,
    CROSSREF_NAME,
    HEADER_DIR,
    MANGLED_DIR,
    OUTPUT_DIR,
    OUTPUT_NAME,
    RAW_DIR,
)
from cursor import cursor
from django.core.management.base import BaseCommand
from sqlalchemy import create_engine
from tqdm import tqdm


class Command(BaseCommand):
    """Base command class for import_uprn."""

    help = "Import raw data from CSV files into the database"

    def show_header(self, text_list, width=80):
        """Show a section Header with an arbitrary number of lines.

        Args:
            text_list (list): A list of Strings to be show, one per line
            width (int, optional): Width to make the box. Defaults to 50.
        """
        divider = "-" * (width - 2)
        self.stdout.write(self.style.HTTP_NOT_MODIFIED("\n/" + divider + "\\"))
        for line in text_list:
            self.stdout.write(
                self.style.HTTP_NOT_MODIFIED(
                    "|" + line.center((width - 2), " ") + "|"
                )
            )
        self.stdout.write(self.style.HTTP_NOT_MODIFIED("\\" + divider + "/"))
        self.stdout.write("\n")

    def phase_one(self):
        """Run phase 1 : Read in the raw CSV and mangle.

        Take the raw CSV files and mangle them into a format that is easier to
        work with, seperate files for each record type.
        """
        self.show_header(["Phase 1", "Mangle the Raw Files"])

        # loop through the header csv files and make a list of the codes and
        # filenames. We are generating this dynamically in case it changes in
        # the future.
        header_files = sorted(glob(os.path.join(HEADER_DIR, "*.csv")))
        # delete the current *.csv here first
        [f.unlink() for f in Path(MANGLED_DIR).glob("*.csv") if f.is_file()]

        # set up the dictionary and create the skeleton files
        code_list = {}
        for filepath in header_files:
            # drop the path
            header_filename = os.path.basename(filepath)
            # get the record number
            record = header_filename.split("Record_")[1].split("_")[0]
            filename = header_filename[:-11] + ".csv"
            # add it to the dictionary with the record as a key
            code_list[record] = filename

            # create an empty file with the contents of the header file
            # we basically just copy the file over and rename
            destpath = os.path.join(MANGLED_DIR, filename)
            copyfile(filepath, destpath)

        # get list of all *csv to process
        input_files = glob(os.path.join(RAW_DIR, "*.csv"))

        # loop over all the files if empty
        for filename in tqdm(input_files, ncols=80, unit=" files"):

            with open(filename) as fp:
                # get the next line
                line = fp.readline()
                while line:
                    # get the record type
                    record = line.split(",")[0]
                    # get the correct output file for this record type
                    output_filename = os.path.join(
                        MANGLED_DIR, code_list[record]
                    )
                    # append this line to the output file
                    with open(output_filename, "a") as f:
                        f.write(line)
                        if record == "99":
                            # record type 99 is always at the end of the file,
                            # so is lacking a LF. Add one.
                            f.write("\n")
                    line = fp.readline()

    def phase_two(self):
        """Run phase 2 : Format as we need and export to CSV for next stage."""
        self.show_header(["Phase 2", "Consolidate data into one CSV."])

        # first we need to get a list of the sorted files.
        mangled_files = sorted(glob(os.path.join(MANGLED_DIR, "*.csv")))

        # get a list of the codes linked to their actual files
        code_list = {}
        for filepath in mangled_files:
            # drop the path
            filename = os.path.basename(filepath)
            # get the record number
            record = filename.split("Record_")[1].split("_")[0]
            # add it to the dictionary with the record as a key
            code_list[record] = filename

        self.stdout.write("Reading in the required Records...")

        # get record 15 (STREETDESCRIPTOR)
        raw_record_15 = pd.read_csv(
            os.path.join(MANGLED_DIR, code_list["15"]),
            usecols=[
                "USRN",
                "STREET_DESCRIPTION",
                "LOCALITY",
                "TOWN_NAME",
                "ADMINISTRATIVE_AREA",
            ],
            dtype={"USRN": "str"},
        )

        # get record 21 (BPLU)
        raw_record_21 = pd.read_csv(
            os.path.join(MANGLED_DIR, code_list["21"]),
            usecols=[
                "UPRN",
                "LOGICAL_STATUS",
                "BLPU_STATE",
                "X_COORDINATE",
                "Y_COORDINATE",
                "LATITUDE",
                "LONGITUDE",
                "COUNTRY",
            ],
            dtype={"BLPU_STATE": "str", "LOGICAL_STATUS": "str"},
        )
        raw_record_21.set_index(["UPRN"], inplace=True)

        # get record 28 (DeliveryPointAddress)
        raw_record_28 = pd.read_csv(
            os.path.join(MANGLED_DIR, code_list["28"]),
            usecols=[
                "UPRN",
                "SUB_BUILDING_NAME",
                "BUILDING_NAME",
                "BUILDING_NUMBER",
                "THOROUGHFARE",
                "POST_TOWN",
                "POSTCODE",
            ],
            dtype={"BUILDING_NUMBER": "str"},
        )
        raw_record_28.set_index(["UPRN"], inplace=True)

        # get record 32 (CLASSIFICATION)
        raw_record_32 = pd.read_csv(
            os.path.join(MANGLED_DIR, code_list["32"]),
            usecols=["UPRN", "CLASSIFICATION_CODE", "CLASS_SCHEME"],
        )
        raw_record_32.set_index(["UPRN"], inplace=True)
        # record 32 has duplicate information for many UPRN, this will cause
        # the concat to fail. We are only interested in the ones that have the
        # scheme named : "AddressBase Premium Classification Scheme"
        filtered_record_32 = raw_record_32[
            raw_record_32.CLASS_SCHEME.str.contains("AddressBase")
        ]

        # now bring in the cross reference file to link UPRN to USRN
        self.stdout.write(" Reading the UPRN <-> USRN reference file")
        cross_ref_file = os.path.join(CROSSREF_DIR, CROSSREF_NAME)
        cross_ref = pd.read_csv(
            cross_ref_file,
            usecols=["IDENTIFIER_1", "IDENTIFIER_2"],
            dtype={"IDENTIFIER_1": "str", "IDENTIFIER_2": "str"},
        )

        # lets rename these 2 headers to the better names
        cross_ref.rename(
            columns={"IDENTIFIER_1": "UPRN", "IDENTIFIER_2": "USRN"},
            inplace=True,
        )

        self.stdout.write(" Merging in the STREETDATA")
        # concat the STREETDESCRIPTOR to the cross ref file in this step
        merged_usrn = pd.merge(
            cross_ref,
            raw_record_15,
            how="left",
            left_on="USRN",
            right_on="USRN",
        )

        self.stdout.write(" Concating data ...")
        chunk1 = pd.concat(
            [
                raw_record_28,
                raw_record_21,
                filtered_record_32.drop(columns=["CLASS_SCHEME"]),
            ],
            axis=1,
        )

        # we dont want it indexed for the next stage, and need to clearly
        # specifiy the UPRN datatype
        chunk1.reset_index(inplace=True)
        merged_usrn.UPRN = merged_usrn.UPRN.astype(int)

        self.stdout.write(" Merging in the Street data ...")
        final_output = pd.merge(
            chunk1,
            merged_usrn,
            how="left",
            left_on="UPRN",
            right_on="UPRN",
        )

        # set the index back onto the UPRN
        final_output.set_index(["UPRN"], inplace=True)

        # finally, save the formatted data to a new CSV file.
        output_file = os.path.join(OUTPUT_DIR, OUTPUT_NAME)
        self.stdout.write(f"\n Saving to {output_file}")
        final_output.to_csv(output_file, index_label="UPRN", sep="|")

    def phase_three(self):
        """Read in the prepared CSV file and then store it in our DB."""
        self.show_header(
            ["Phase 3", "Load to database", "This may take a LONG time!!"]
        )

        self.stdout.write(" Importing the Formatted AddressBase CSV file...")
        ab_data = pd.read_csv(
            os.path.join(OUTPUT_DIR, OUTPUT_NAME),
            # lets spell out the exact column types for clarity
            na_filter=False,
            sep="|",
            dtype={
                "UPRN": "int",
                "SUB_BUILDING_NAME": "str",
                "BUILDING_NAME": "str",
                "BUILDING_NUMBER": "str",
                "THOROUGHFARE": "str",
                "POST_TOWN": "str",
                "POSTCODE": "str",
                "LOGICAL_STATUS": "int",
                # needs to be a string as annoyingly the data includes null
                # values
                "BLPU_STATE": "str",
                "X_COORDINATE": "double",
                "X_COORDINATE": "double",
                "LATITUDE": "double",
                "LONGITUDE": "double",
                "COUNTRY": "str",
                "CLASSIFICATION_CODE": "str",
                # also contains Null values for demolished buildings so must
                # be a string
                "USRN": "str",
                "STREET_DESCRIPTION": "str",
                "LOCALITY": "str",
                "TOWN_NAME": "str",
                "ADMINISTRATIVE_AREA": "str",
            },
        )

        # at this point we want to create an extra field in the DataFrame, with
        # the address data concated for easier display.
        ab_data.insert(1, "FULL_ADDRESS", "")

        # now create a clean combined address from the relevant fields
        # doing this in 2 runs so we can sort out formatting in the first due
        # to any missing data.
        self.stdout.write(" Combining Address Fields...")
        ab_data["FULL_ADDRESS"] = (
            ab_data["SUB_BUILDING_NAME"]
            .str.cat(
                ab_data[
                    [
                        "BUILDING_NAME",
                        "BUILDING_NUMBER",
                        "THOROUGHFARE",
                    ]
                ],
                sep=" ",
            )
            .str.strip()  # trim extra space
            .str.title()  # convert to Title Case
        )
        # add the rest...
        ab_data["FULL_ADDRESS"] = (
            ab_data["FULL_ADDRESS"]
            .str.cat(ab_data["POST_TOWN"].str.title(), sep=", ")
            .str.cat(
                ab_data["POSTCODE"], sep=", "
            )  # no Title mod for the Postcode
            .str.cat(ab_data["ADMINISTRATIVE_AREA"].str.title(), sep=", ")
        )

        # create a postgresql engine with SQLAlchemy that is linked to our
        # database
        db_url = "postgresql://{}:{}@{}:{}/{}".format(
            str(os.getenv("UPRN_DB_USER")),
            str(os.getenv("UPRN_DB_PASSWORD")),
            str(os.getenv("UPRN_DB_HOST")),
            str(os.getenv("UPRN_DB_PORT")),
            str(os.getenv("UPRN_DB_NAME")),
        )
        engine = create_engine(db_url)

        # now use the Pandas to_sql function to write to the database...
        # this will take a long time (Scotland is 5.7 Million rows for example
        # and this takes 25 minutes on my decent PC). There are quicker ways to
        # dothis which I will look at later once the scripts are proven and
        # trusted.
        self.stdout.write(
            " Exporting data to the Postgresql database "
            "... this will take a while"
        )
        ab_data.to_sql(
            str(os.getenv("UPRN_DB_TABLE")),
            engine,
            if_exists="replace",
            index=False,
            chunksize=10000,
            method="multi",
        )

    def handle(self, *args, **options):
        """Actual function called by the command."""
        cursor.hide()

        self.phase_one()
        self.phase_two()
        self.phase_three()

        cursor.show()
